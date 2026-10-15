from app.main import ANALYTICS_OPTIONS, department_summary_pipeline, employee_monthly_pipeline, late_leaderboard_pipeline
from test_phase1 import instant


def seed_employee(db, code, department="Engineering", joined="2026-07-01", name=None):
    db.employees.insert_one({
        "emp_code": code, "name": name or code, "email": f"{code.lower()}@example.com",
        "department": department, "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
    })


def seed_log(db, code, day, status="PRESENT", **fields):
    db.attendance_logs.insert_one({"emp_code": code, "date": day, "status": status, **fields})


def test_employee_monthly_zero_logs_joiners_leap_and_rollover(api):
    client, db = api
    seed_employee(db, "EMP0001", joined="2024-02-01")
    seed_employee(db, "EMP0002", joined="2024-02-15")
    seed_employee(db, "EMP0003", joined="2024-03-01")
    seed_employee(db, "EMP0004", joined="2025-01-01")
    expected = {
        "EMP0001": 21, "EMP0002": 11, "EMP0003": 0,
    }
    for code, days in expected.items():
        response = client.get(f"/analytics/employees/{code}/monthly", params={"month": "2024-02"})
        assert response.status_code == 200, response.text
        assert response.json() == {
            "emp_code": code, "month": "2024-02", "working_days": days,
            "present_days": 0.0, "leave_days": 0, "late_count": 0,
            "total_late_minutes": 0, "total_overtime_minutes": 0,
            "attendance_pct": None if days == 0 else 0.0,
        }
    december = client.get("/analytics/employees/EMP0004/monthly", params={"month": "2024-12"})
    assert december.status_code == 200 and december.json()["working_days"] == 0
    january = client.get("/analytics/employees/EMP0004/monthly", params={"month": "2025-01"})
    assert january.status_code == 200 and january.json()["working_days"] == 23
    assert client.get("/analytics/employees/UNKNOWN/monthly", params={"month": "2024-02"}).status_code == 404
    for bad in ("2024-00", "2024-13", "2024-2", "0000-01", "2024-02-01", "junk"):
        assert client.get("/analytics/employees/EMP0001/monthly", params={"month": bad}).status_code == 422
    assert client.get("/analytics/employees/EMP0001/monthly").status_code == 422


def test_employee_and_department_metrics_from_stored_values(api):
    client, db = api
    seed_employee(db, "EMP0001")
    seed_employee(db, "EMP0002")
    seed_employee(db, "EMP0003")  # Headcount with zero attendance.
    seed_employee(db, "EMP0004", joined="2026-08-01")  # Excluded from July headcount.
    seed_employee(db, "EMP0005", department="Sales")
    # Timestamps imply a normal nine-hour day, but stored analytics values are authoritative.
    seed_log(db, "EMP0001", "2026-07-06", half_day=True, work_hours=1.0, late_minutes=12,
             overtime_minutes=10, punch_in=instant("2026-07-06", "09:30:00"),
             punch_out=instant("2026-07-06", "18:30:00"))
    seed_log(db, "EMP0001", "2026-07-07", status="WFH", work_hours=3.0, late_minutes=0, overtime_minutes=0)
    seed_log(db, "EMP0001", "2026-07-11", status="ON_DUTY", work_hours=10.0, late_minutes=28, overtime_minutes=40)
    seed_log(db, "EMP0001", "2026-07-08", status="ABSENT")
    seed_log(db, "EMP0001", "2026-07-09", status="LEAVE")
    seed_log(db, "EMP0002", "2026-07-06", work_hours=8.0)  # Legacy missing derived totals.
    seed_log(db, "EMP0005", "2026-07-06", status="LEAVE")
    seed_log(db, "EMP0004", "2026-07-06", work_hours=12.0, late_minutes=999)
    employee = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert employee.status_code == 200, employee.text
    assert employee.json() == {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 23,
        "present_days": 1.5, "leave_days": 1, "late_count": 2,
        "total_late_minutes": 40, "total_overtime_minutes": 50, "attendance_pct": 6.52,
    }
    summary = client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert summary.status_code == 200, summary.text
    assert summary.json() == {"month": "2026-07", "items": [
        {"department": "Engineering", "headcount": 3, "present_days": 2.5,
         "avg_work_hours": 5.5, "late_count": 2, "total_late_minutes": 40,
         "leave_count": 1, "on_duty_count": 1},
        {"department": "Sales", "headcount": 1, "present_days": 0.0,
         "avg_work_hours": None, "late_count": 0, "total_late_minutes": 0,
         "leave_count": 1, "on_duty_count": 0},
    ]}
    assert client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "engineering"}).json()["items"] == []
    assert client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Sales"}).json()["items"][0]["headcount"] == 1
    assert client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"}).json()["items"][0]["headcount"] == 3
    assert client.get("/analytics/departments/summary", params={"month": "2026-06"}).json()["items"] == []
    assert client.get("/analytics/departments/summary", params={"month": "2026-07-01"}).status_code == 422


def test_half_up_rate_average_and_open_record(api):
    client, db = api
    seed_employee(db, "EMP0010", department="Rounding", joined="2026-07-10")
    seed_log(db, "EMP0010", "2026-07-10", half_day=True, work_hours=1.0)
    seed_log(db, "EMP0010", "2026-07-13", status="WFH", half_day=False, work_hours=1.01)
    # 1.5 / 16 * 100 = 9.375; half-up to 9.38. Average 1.005 -> 1.01.
    result = client.get("/analytics/employees/EMP0010/monthly", params={"month": "2026-07"})
    assert result.status_code == 200 and result.json()["working_days"] == 16
    assert result.json()["attendance_pct"] == 9.38
    summary = client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert summary.status_code == 200, summary.text
    assert summary.json()["items"][0]["avg_work_hours"] == 1.01
    seed_employee(db, "EMP0011", department="Open")
    seed_log(db, "EMP0011", "2026-07-14", status="PRESENT", work_hours=None)
    open_summary = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Open"})
    assert open_summary.json()["items"][0]["avg_work_hours"] is None
    assert open_summary.json()["items"][0]["present_days"] == 1.0


def test_leaderboard_ties_skipped_rank_department_and_orphans(api):
    client, db = api
    seed_employee(db, "EMP0001", department="Sales", name="A")
    seed_employee(db, "EMP0002", name="B")
    seed_employee(db, "EMP0003", name="C")
    seed_employee(db, "EMP0004", name="D")
    seed_log(db, "EMP0001", "2026-07-06", late_minutes=100)
    seed_log(db, "EMP0002", "2026-07-06", late_minutes=20)
    seed_log(db, "EMP0002", "2026-07-11", late_minutes=30)  # Weekend counts.
    seed_log(db, "EMP0003", "2026-07-06", late_minutes=50)
    seed_log(db, "EMP0004", "2026-07-06", late_minutes=10)
    seed_log(db, "ORPHAN", "2026-07-06", late_minutes=1000)
    seed_log(db, "EMP0004", "2026-08-01", late_minutes=500)
    result = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2})
    assert result.status_code == 200, result.text
    assert result.json() == {"month": "2026-07", "items": [
        {"rank": 1, "emp_code": "EMP0001", "name": "A", "department": "Sales", "total_late_minutes": 100, "late_count": 1},
        {"rank": 2, "emp_code": "EMP0002", "name": "B", "department": "Engineering", "total_late_minutes": 50, "late_count": 2},
        {"rank": 2, "emp_code": "EMP0003", "name": "C", "department": "Engineering", "total_late_minutes": 50, "late_count": 1},
    ]}
    assert [item["rank"] for item in client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4}).json()["items"]] == [1, 2, 2, 4]
    local = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": "Engineering", "limit": 1})
    assert [(item["rank"], item["emp_code"]) for item in local.json()["items"]] == [(1, "EMP0002"), (1, "EMP0003")]
    assert client.get("/analytics/leaderboard/late", params={"month": "2026-06"}).json() == {"month": "2026-06", "items": []}
    for params in ({"month": "2026-07", "limit": 0}, {"month": "2026-07", "limit": 51}, {"month": "2026-07", "limit": "abc"}, {"month": "2026-13"}):
        assert client.get("/analytics/leaderboard/late", params=params).status_code == 422
    assert client.get("/analytics/leaderboard/late").status_code == 422


def scan_stages(value):
    if isinstance(value, dict):
        if isinstance(value.get("stage"), str):
            yield value["stage"]
        for child in value.values():
            yield from scan_stages(child)
    elif isinstance(value, list):
        for child in value:
            yield from scan_stages(child)


def test_pipeline_execution_plans_use_indexes(api):
    _, db = api
    seed_employee(db, "EMP0001")
    seed_log(db, "EMP0001", "2026-07-06", late_minutes=10, work_hours=8.0)
    cases = [
        ("employees", employee_monthly_pipeline("EMP0001", "2026-07")),
        ("employees", department_summary_pipeline("2026-07", "Engineering")),
        ("employees", department_summary_pipeline("2026-07", None)),
        ("attendance_logs", late_leaderboard_pipeline("2026-07", 10, None)),
        ("attendance_logs", late_leaderboard_pipeline("2026-07", 10, "Engineering")),
    ]
    for collection, pipeline in cases:
        plan = db.command({"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}, **ANALYTICS_OPTIONS}, "verbosity": "executionStats"})
        stages = list(scan_stages(plan))
        assert "IXSCAN" in stages, (collection, stages)
        assert "COLLSCAN" not in stages, (collection, stages)
        lookups = [stage for stage in plan.get("stages", []) if "$lookup" in stage]
        for lookup in lookups:
            assert lookup.get("collectionScans") == 0, (collection, lookup)
            assert lookup.get("indexesUsed"), (collection, lookup)
