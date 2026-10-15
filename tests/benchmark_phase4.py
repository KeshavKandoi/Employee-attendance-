"""Opt-in 100k-record integration benchmark: pytest -s -q tests/benchmark_phase4.py."""

import json
import time
import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.main import app
from test_phase3 import scan_stages


def test_large_dataset_startup_queries_and_plans(mongo_server, monkeypatch):
    database_name = "hrone_benchmark_" + uuid.uuid4().hex
    monkeypatch.setenv("MONGO_URI", mongo_server)
    monkeypatch.setenv("MONGO_DB", database_name)
    client = MongoClient(mongo_server)
    db = client[database_name]
    departments = ["Engineering", "Sales", "Support", "Finance"]
    employees = [
        {"emp_code": f"EMP{number:04d}", "name": f"Employee {number}",
         "email": f"employee{number}@example.com", "department": departments[number % 4],
         "joined_on": ["2025-01-01", "2025-06-01", "2026-01-01"][number % 3],
         "shift_start": "09:30", "shift_end": "18:30"}
        for number in range(500)
    ]
    try:
        db.employees.insert_many(employees)
        first_day = date(2026, 1, 1)
        batch = []
        for offset in range(200):
            day = (first_day + timedelta(days=offset)).isoformat()
            for number in range(500):
                batch.append({
                    "emp_code": f"EMP{number:04d}", "date": day,
                    "status": ["PRESENT", "WFH", "ON_DUTY", "LEAVE"][number % 4],
                    "work_hours": 4.0 if number % 10 == 0 else 8.0,
                    "half_day": number % 10 == 0,
                    "late_minutes": 15 if (number + offset) % 3 == 0 else 0,
                    "overtime_minutes": 30 if number % 5 == 0 else 0,
                })
                if len(batch) == 1000:
                    db.attendance_logs.insert_many(batch)
                    batch.clear()
        assert db.attendance_logs.count_documents({}) == 100_000
        started = time.perf_counter()
        with TestClient(app) as api:
            startup_seconds = time.perf_counter() - started
            assert api.get("/health").status_code == 200
            queries = [
                ("attendance_list", "/attendance", {}),
                ("attendance_filtered", "/attendance", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-19", "page": 2, "page_size": 5}),
                ("employee_monthly", "/analytics/employees/EMP0001/monthly", {"month": "2026-07"}),
                ("department_summary", "/analytics/departments/summary", {"month": "2026-07"}),
                ("late_leaderboard", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 10}),
                ("department_trend", "/analytics/departments/Engineering/trend", {"from": "2026-04-19", "to": "2026-07-19"}),
            ]
            timings = {}
            for name, path, params in queries:
                began = time.perf_counter()
                response = api.get(path, params=params)
                timings[name] = round(time.perf_counter() - began, 3)
                assert response.status_code == 200, (name, response.text[:500])
            explains = [
                ("attendance_list", {}),
                ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
                ("department_summary", {"month": "2026-07"}),
                ("late_leaderboard", {"month": "2026-07", "limit": 10}),
                ("department_trend", {"department": "Engineering", "from": "2026-04-19", "to": "2026-07-19"}),
            ]
            plans = {}
            for name, params in explains:
                began = time.perf_counter()
                response = api.get(f"/admin/explain/{name}", params=params)
                assert response.status_code == 200, (name, response.text[:500])
                explain = response.json()["explain"]
                stages = list(scan_stages(explain))
                lookups = [stage for stage in explain.get("stages", []) if "$lookup" in stage]
                plans[name] = {
                    "seconds": round(time.perf_counter() - began, 3),
                    "index_stages": sorted({stage for stage in stages if stage.endswith("IXSCAN")}),
                    "collection_scans": stages.count("COLLSCAN"),
                    "lookup_collection_scans": sum(stage.get("collectionScans", 0) for stage in lookups),
                }
                assert plans[name]["index_stages"], (name, stages)
                assert plans[name]["collection_scans"] == 0, (name, stages)
                assert plans[name]["lookup_collection_scans"] == 0, (name, lookups)
            print("BENCHMARK " + json.dumps({"records": 100000, "employees": 500, "startup_seconds": round(startup_seconds, 3), "route_seconds": timings, "explain": plans}, sort_keys=True))
            assert startup_seconds < 20
    finally:
        client.drop_database(database_name)
        client.close()
