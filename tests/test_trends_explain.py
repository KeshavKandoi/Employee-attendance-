import json

from bson import json_util

from app.main import (
    ANALYTICS_OPTIONS, attendance_list_operation, department_summary_pipeline,
    department_trend_pipeline, employee_monthly_pipeline, late_leaderboard_pipeline,
)
from test_attendance_core import instant
from test_analytics import scan_stages, seed_employee, seed_log


def trend(client, department="Engineering", start="2026-07-06", end="2026-07-14"):
    return client.get(f"/analytics/departments/{department}/trend", params={"from": start, "to": end})


def test_trend_gap_filling_headcount_weekends_and_window(api):
    client, db = api
    seed_employee(db, "EMP0001", joined="2026-07-01")
    seed_employee(db, "EMP0002", joined="2026-07-08")
    seed_employee(db, "EMP0003", joined="2026-07-20")
    seed_log(db, "EMP0001", "2026-07-07", status="PRESENT", half_day=True, late_minutes=12,
             punch_in=instant("2026-07-07", "09:30:00"))
    seed_log(db, "EMP0001", "2026-07-08", status="WFH", half_day=False)
    seed_log(db, "EMP0002", "2026-07-08", status="ON_DUTY", half_day=False, late_minutes=0)
    seed_log(db, "EMP0001", "2026-07-09", status="PRESENT")
    seed_log(db, "EMP0002", "2026-07-11", status="PRESENT", half_day=True, late_minutes=5)
    result = trend(client)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["department"] == "Engineering"
    rows = data["items"]
    assert len(rows) == 9
    assert [r["date"] for r in rows] == [f"2026-07-{day:02d}" for day in range(6, 15)]
    assert set(rows[0]) == {"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"}
    expected = [
        (1, 0.0, 0, True, 0.0, 0.0),
        (1, 0.5, 1, True, 0.5, 0.25),
        (2, 2.0, 0, True, 1.0, 0.5),
        (2, 1.0, 0, True, 0.5, 0.5),
        (2, 0.0, 0, True, 0.0, 0.4),
        (2, 0.5, 1, False, None, 0.4),
        (2, 0.0, 0, False, None, 0.4),
        (2, 0.0, 0, True, 0.0, 0.4),
        (2, 0.0, 0, True, 0.0, 0.3),
    ]
    for row, (headcount, present, late, working, rate, moving) in zip(rows, expected):
        assert (row["headcount"], row["present_count"], row["late_count"], row["is_working_day"], row["attendance_rate"], row["moving_avg_7d"]) == (headcount, present, late, working, rate, moving)

    shorter = trend(client, start="2026-07-08", end="2026-07-14").json()["items"]
    assert shorter[0]["attendance_rate"] == 1.0 and shorter[0]["moving_avg_7d"] == 1.0


def test_trend_empty_ranges_future_joiners_and_validation(api):
    client, db = api
    seed_employee(db, "EMP0001", joined="2026-08-01")
    one = trend(client, start="2026-07-06", end="2026-07-06")
    assert one.status_code == 200, one.text
    assert one.json()["items"] == [{
        "date": "2026-07-06", "is_working_day": True, "headcount": 0,
        "present_count": 0.0, "late_count": 0, "attendance_rate": None, "moving_avg_7d": None,
    }]
    weekend = trend(client, start="2026-07-11", end="2026-07-12")
    assert weekend.status_code == 200
    assert all(row["moving_avg_7d"] is None for row in weekend.json()["items"])
    full = trend(client, start="2026-01-01", end="2026-04-02")
    assert full.status_code == 200 and len(full.json()["items"]) == 92
    assert full.json()["items"][0]["date"] == "2026-01-01"
    assert full.json()["items"][-1]["date"] == "2026-04-02"
    assert trend(client, start="2026-01-01", end="2026-04-03").status_code == 422
    assert trend(client, start="2026-07-08", end="2026-07-07").status_code == 422
    assert trend(client, start="2026-02-30", end="2026-03-01").status_code == 422
    assert trend(client, department="engineering").status_code == 404
    assert client.get("/analytics/departments/Engineering/trend", params={"to": "2026-07-06"}).status_code == 422
    assert client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06"}).status_code == 422


def test_trend_four_decimal_half_up_rate_and_moving_average(api):
    client, db = api
    for number in range(1, 4):
        seed_employee(db, f"EMP{number:04d}", joined="2026-07-01")
    seed_log(db, "EMP0001", "2026-07-06")
    seed_log(db, "EMP0001", "2026-07-07")
    seed_log(db, "EMP0002", "2026-07-07", half_day=True)
    rows = trend(client, start="2026-07-06", end="2026-07-07").json()["items"]
    assert [row["attendance_rate"] for row in rows] == [0.3333, 0.5]
    assert [row["moving_avg_7d"] for row in rows] == [0.3333, 0.4167]

    for number in range(4, 17):
        seed_employee(db, f"EMP{number:04d}", joined="2026-07-01")
    seed_log(db, "EMP0001", "2026-07-08", half_day=True)
    tie = trend(client, start="2026-07-08", end="2026-07-08").json()["items"][0]
    assert tie["headcount"] == 16 and tie["attendance_rate"] == 0.0313
    assert tie["moving_avg_7d"] == 0.0313


def test_explain_validation_parity_and_index_plans(api):
    client, db = api
    seed_employee(db, "EMP0001")
    seed_log(db, "EMP0001", "2026-07-06", late_minutes=5)
    requests = [
        ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT", "page": 2, "page_size": 5}, "attendance_logs"),
        ("attendance_list", {}, "attendance_logs"),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "employees"),
        ("department_summary", {"month": "2026-07", "department": "Engineering"}, "employees"),
        ("department_summary", {"month": "2026-07"}, "employees"),
        ("late_leaderboard", {"month": "2026-07", "department": "Engineering", "limit": 2}, "attendance_logs"),
        ("late_leaderboard", {"month": "2026-07"}, "attendance_logs"),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-08"}, "employees"),
    ]
    for name, params, collection in requests:
        response = client.get(f"/admin/explain/{name}", params=params)
        assert response.status_code == 200, (name, response.text)
        data = response.json()
        assert set(data) == {"endpoint", "collection", "explain"}
        assert data["endpoint"] == name and data["collection"] == collection
        plan = data["explain"]
        assert "executionStats" in str(plan)
        stages = list(scan_stages(plan))
        assert any(stage.endswith("IXSCAN") for stage in stages) and "COLLSCAN" not in stages, (name, stages)
        lookups = [stage for stage in plan.get("stages", []) if "$lookup" in stage]
        assert all(stage.get("collectionScans") == 0 for stage in lookups), (name, lookups)
        command = plan.get("command", {})
        if name == "attendance_list":
            expected = attendance_list_operation(params.get("emp_code"), params.get("date_from"), params.get("date_to"), params.get("status"), params.get("page", 1), params.get("page_size", 20))
            assert command.get("find") == collection
            assert command.get("filter") == expected["filter"]
            assert command.get("sort") == expected["sort"]
            assert command.get("skip", 0) == expected["skip"]
            assert command.get("limit") == expected["limit"]
        else:
            expected = {
                "employee_monthly": lambda: employee_monthly_pipeline(params["emp_code"], params["month"]),
                "department_summary": lambda: department_summary_pipeline(params["month"], params.get("department")),
                "late_leaderboard": lambda: late_leaderboard_pipeline(params["month"], params.get("limit", 10), params.get("department")),
                "department_trend": lambda: department_trend_pipeline(params["department"], params["from"], params["to"]),
            }[name]()
            assert command.get("aggregate") == collection
            assert command.get("pipeline") == json.loads(json_util.dumps(expected))
            assert command.get("allowDiskUse") == ANALYTICS_OPTIONS["allowDiskUse"]
    for name, params in [
        ("unknown", {}), ("employee_monthly", {"month": "2026-07"}),
        ("employee_monthly", {"emp_code": "EMP0001"}), ("department_summary", {}),
        ("late_leaderboard", {}), ("department_trend", {}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-06"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-08", "to": "2026-07-07"}),
        ("attendance_list", {"page": 0}), ("attendance_list", {"status": "WRONG"}),
        ("attendance_list", {"date_from": "2026-07-08", "date_to": "2026-07-07"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 51}),
    ]:
        assert client.get(f"/admin/explain/{name}", params=params).status_code == 422
