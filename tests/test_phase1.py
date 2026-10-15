from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from pymongo.errors import ConnectionFailure

from app.main import (
    IST, attendance_date, compute_half_day, compute_late_minutes, compute_overtime,
    compute_work_hours, epoch_ms_to_utc, shift_instants, truncate_seconds, utc_to_epoch_ms,
)


def instant(day, clock):
    return datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=IST).astimezone(timezone.utc)


def employee(code="EMP0001", department="Engineering", **extra):
    return {"emp_code": code, "name": "Asha", "email": "asha@example.com", "department": department, "joined_on": "2026-07-01", **extra}


def test_time_boundaries():
    day = "2026-07-06"
    assert compute_late_minutes(instant(day, "09:40:00"), "09:30", day) == 0
    assert compute_late_minutes(instant(day, "09:40:01"), "09:30", day) == 10
    assert compute_late_minutes(instant(day, "09:40:00.900"), "09:30", day) == 0
    assert compute_work_hours(instant(day, "09:00:00"), instant(day, "10:00:18")) == 1.01
    assert compute_overtime(instant(day, "18:35:00"), "18:30", day, "09:30") == 0
    assert compute_overtime(instant("2026-07-07", "06:40:00"), "06:00", day, "22:00") == 40
    assert compute_half_day(4.49) and not compute_half_day(4.50)
    assert attendance_date(instant(day, "00:00:00"), "09:30", "18:30") == day
    assert attendance_date(instant("2026-07-07", "05:59:59"), "22:00", "06:00") == day
    assert attendance_date(instant("2026-07-07", "06:00:00"), "22:00", "06:00") == "2026-07-07"
    assert shift_instants(day, "22:00", "06:00")[1] == instant("2026-07-07", "06:00:00")
    assert utc_to_epoch_ms(epoch_ms_to_utc(1783312500000)) == 1783312500000
    assert truncate_seconds(epoch_ms_to_utc(1783312500900)).microsecond == 0


def test_employee_validation_and_listing(api):
    client, db = api
    bad = [
        {"emp_code": "BAD"}, {"name": ""}, {"email": "bad"},
        {"department": ""}, {"joined_on": "2026-02-30"},
        {"joined_on": "2026-7-01"}, {"shift_start": "25:00"},
        {"shift_end": "09:30"},
    ]
    for patch in bad:
        assert client.post("/employees", json={**employee(), **patch}).status_code == 422
    assert client.post("/employees", json=employee("EMP0003", "Sales")).status_code == 201
    assert client.post("/employees", json=employee("EMP0001")).status_code == 201
    assert client.post("/employees", json=employee("EMP0002")).status_code == 201
    response = client.get("/employees", params={"page_size": 2}).json()
    assert response["total"] == 3
    assert [x["emp_code"] for x in response["items"]] == ["EMP0001", "EMP0002"]
    assert [x["emp_code"] for x in client.get("/employees", params={"page": 2, "page_size": 2}).json()["items"]] == ["EMP0003"]
    assert client.get("/employees", params={"department": "Sales"}).json()["total"] == 1
    assert client.get("/employees", params={"department": "sales"}).json()["total"] == 0
    for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
        assert client.get("/employees", params=params).status_code == 422
    assert "_id" not in response["items"][0] and "id" not in response["items"][0]
    assert isinstance(response["items"][0]["created_at"], int)
    assert db.employees.index_information()["emp_code_1"]["unique"]


def test_concurrent_employee_create(api):
    client, _ = api
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.post("/employees", json=employee()).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7


def test_punch_in_validation_and_concurrency(api):
    client, db = api
    assert client.post("/employees", json=employee()).status_code == 201
    timestamp = utc_to_epoch_ms(instant("2026-07-06", "09:40:00.900"))
    for value in (None, "1783312500000", 1783312500000.0, True, 1783312500, 99999999999, 4102444800001):
        assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422
    for status in ("ABSENT", "LEAVE", "bad"):
        assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status}).status_code == 422
    assert client.post("/attendance/punch-in", json={"emp_code": "UNKNOWN", "punched_at": timestamp}).status_code == 404
    body = {"emp_code": "EMP0001", "punched_at": timestamp, "status": "WFH", "late_minutes": 999}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: client.post("/attendance/punch-in", json=body), range(8)))
    assert sorted(result.status_code for result in results) == [201] + [409] * 7
    item = next(result.json() for result in results if result.status_code == 201)
    assert item == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "WFH", "punch_in": timestamp - 900, "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    assert "_id" not in item and "id" not in item
    assert db.attendance_logs.index_information()["emp_code_1_date_1"]["unique"]


def test_omitted_timestamp_and_overnight_date(api):
    client, db = api
    assert client.post("/employees", json=employee("EMP0005", shift_start="22:00", shift_end="06:00")).status_code == 201
    timestamp = utc_to_epoch_ms(instant("2026-07-07", "05:59:59.900"))
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": timestamp})
    assert response.status_code == 201
    assert response.json()["date"] == "2026-07-06"
    assert response.json()["punch_in"] == timestamp - 900
    assert db.attendance_logs.find_one({"emp_code": "EMP0005"})["date"] == "2026-07-06"
    # A missing timestamp uses the server clock, even though an explicit null is invalid.
    assert client.post("/employees", json=employee("EMP0006")).status_code == 201
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0006"}).status_code == 201


def test_health_unavailable(api, monkeypatch):
    client, _ = api
    assert client.get("/health").json() == {"status": "ok"}

    class Unavailable:
        def command(self, name):
            raise ConnectionFailure("test outage")

    from app.main import app
    monkeypatch.setattr(app.state, "db", Unavailable())
    assert client.get("/health").status_code == 503


def test_attendance_filters_serialization_and_legacy(api):
    client, db = api
    moment = instant("2026-07-06", "09:00:00")
    records = [
        {"emp_code": "EMP0002", "date": "2026-07-07", "status": "PRESENT", "punch_in": moment},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "LEAVE"},
        {"emp_code": "EMP0003", "date": "2026-07-06", "status": "PRESENT", "punch_in": moment,
         "history": [{"at": moment, "by": "hr", "reason": "fix", "changes": {"punch_in": {"from": moment, "to": None}}}]},
    ]
    db.attendance_logs.insert_many(records)
    first = client.get("/attendance", params={"page_size": 2}).json()
    assert first["total"] == 3
    assert [(x["date"], x["emp_code"]) for x in first["items"]] == [("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    second = client.get("/attendance", params={"page": 2, "page_size": 2}).json()
    assert len(second["items"]) == 1
    legacy = second["items"][0]
    assert legacy["punch_in"] == utc_to_epoch_ms(moment)
    assert legacy["punch_out"] is None and legacy["work_hours"] is None
    assert legacy["late_minutes"] == 0 and legacy["overtime_minutes"] == 0
    assert legacy["half_day"] is False
    assert legacy["history"][0]["at"] == utc_to_epoch_ms(moment)
    assert legacy["history"][0]["changes"]["punch_in"] == {"from": utc_to_epoch_ms(moment), "to": None}
    assert "_id" not in legacy and "id" not in legacy
    assert client.get("/attendance", params={"status": "PRESENT"}).json()["total"] == 2
    assert client.get("/attendance", params={"emp_code": "EMP0001"}).json()["total"] == 1
    assert client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"}).json()["total"] == 2
    for params in ({"date_from": "2026-07-08", "date_to": "2026-07-07"}, {"date_from": "2026-02-30"}, {"status": "WRONG"}, {"page": 0}, {"page_size": 101}):
        assert client.get("/attendance", params=params).status_code == 422
