from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from pymongo.collection import Collection

from app.main import utc_to_epoch_ms
from test_phase1 import employee, instant

DAY = "2026-07-06"


def ms(day, clock):
    return utc_to_epoch_ms(instant(day, clock))


def add_employee(client, code="EMP0001", **extra):
    assert client.post("/employees", json=employee(code, **extra)).status_code == 201


def add_record(db, code="EMP0001", day=DAY, status="PRESENT", punch_in="09:30:00", **extra):
    doc = {"emp_code": code, "date": day, "status": status}
    if punch_in is not None:
        doc["punch_in"] = instant(day, punch_in)
    doc.update(extra)
    db.attendance_logs.insert_one(doc)
    return doc


def patch(client, body, code="EMP0001", day=DAY):
    return client.patch(f"/attendance/{code}/{day}", json={"reason": "manual correction", "regularized_by": "hr.admin", **body})


def assert_shape(data):
    assert set(data) == {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"}
    assert "_id" not in str(data) and "_version" not in str(data)


def test_punch_out_normal_thresholds_and_boundaries(api):
    client, db = api
    add_employee(client)
    add_record(db, late_minutes=17)
    before = ms(DAY, "18:59:59.900")
    result = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": before})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["punch_out"] == before - 900
    assert data["work_hours"] == 9.5
    assert data["late_minutes"] == 17  # Punch-out does not rewrite stored late minutes.
    assert data["overtime_minutes"] == 0  # 29:59 after shift end
    assert data["half_day"] is False and data["history"] == []
    assert_shape(data)
    assert db.attendance_logs.find_one({"emp_code": "EMP0001"})["punch_out"] == instant(DAY, "18:59:59")

    add_employee(client, "EMP0002")
    add_record(db, "EMP0002")
    result = client.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms(DAY, "19:00:00")})
    assert result.status_code == 200 and result.json()["overtime_minutes"] == 30

    add_employee(client, "EMP0003")
    add_record(db, "EMP0003", punch_in="09:00:00")
    result = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": ms("2026-07-07", "09:00:00")})
    assert result.status_code == 200
    assert result.json()["work_hours"] == 24.0


def test_punch_out_overnight_and_half_day(api):
    client, db = api
    add_employee(client, "EMP0005", shift_start="22:00", shift_end="06:00")
    add_record(db, "EMP0005", punch_in="21:55:00")
    result = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms("2026-07-07", "06:40:00")})
    assert result.status_code == 200
    assert result.json()["work_hours"] == 8.75
    assert result.json()["overtime_minutes"] == 40
    add_employee(client, "EMP0006")
    add_record(db, "EMP0006", punch_in="09:30:00")
    result = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": ms(DAY, "13:59:59")})
    assert result.status_code == 200 and result.json()["work_hours"] == 4.5
    assert result.json()["half_day"] is False  # rounded 4.50, despite raw hours below 4.50
    add_employee(client, "EMP0007")
    add_record(db, "EMP0007", punch_in="09:30:00")
    result = client.post("/attendance/punch-out", json={"emp_code": "EMP0007", "punched_at": ms(DAY, "13:59:41")})
    assert result.status_code == 200 and result.json()["work_hours"] == 4.49
    assert result.json()["half_day"] is True


def test_punch_out_errors_and_latest_closed(api):
    client, db = api
    add_employee(client)
    assert client.post("/attendance/punch-out", json={"emp_code": "UNKNOWN"}).status_code == 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(DAY, "10:00:00")}).status_code == 404
    add_record(db)
    for value in (None, "1783312500000", 1783312500000.0, True, 1783312500, 99999999999, 4102444800001):
        assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422
    for clock in ("09:29:59", "09:30:00"):
        assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(DAY, clock)}).status_code in (404, 422)
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "09:30:01")}).status_code == 422
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "punch_in": instant("2026-07-07", "09:00:00"), "punch_out": instant("2026-07-07", "17:00:00")})
    result = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07", "18:00:00")})
    assert result.status_code == 409
    assert db.attendance_logs.find_one({"emp_code": "EMP0001", "date": DAY}).get("punch_out") is None


def test_regularization_recalculation_audit_and_repeated_history(api):
    client, db = api
    add_employee(client)
    add_record(db, punch_in="10:05:00", punch_out=instant(DAY, "18:30:00"), work_hours=8.42, late_minutes=35, overtime_minutes=0, half_day=False)
    result = patch(client, {"punch_in": ms(DAY, "09:28:00.900"), "work_hours": 999})
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["punch_in"] == ms(DAY, "09:28:00")
    assert data["late_minutes"] == 0 and data["work_hours"] == 9.03
    assert set(data["history"][0]["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    assert data["history"][0]["changes"]["punch_in"] == {"from": ms(DAY, "10:05:00"), "to": ms(DAY, "09:28:00")}
    assert isinstance(data["history"][0]["at"], int)
    assert_shape(data)
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert stored["history"][0]["changes"]["punch_in"]["from"] == instant(DAY, "10:05:00")
    assert patch(client, {"punch_in": ms(DAY, "09:28:00")}).status_code == 422
    second = patch(client, {"status": "WFH"})
    assert second.status_code == 200 and len(second.json()["history"]) == 2
    assert second.json()["history"][1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_regularization_absent_leave_presence(api):
    client, db = api
    add_employee(client)
    add_record(db, punch_out=instant(DAY, "18:30:00"), work_hours=9.0, half_day=False)
    for status in ("ABSENT", "LEAVE"):
        result = patch(client, {"status": status})
        assert result.status_code == 200
        data = result.json()
        assert data["status"] == status and data["punch_in"] is None and data["punch_out"] is None
        assert data["work_hours"] is None and data["late_minutes"] == 0
        assert data["overtime_minutes"] == 0 and data["half_day"] is False
    assert patch(client, {"status": "PRESENT"}).status_code == 422
    assert patch(client, {"status": "PRESENT", "punch_in": None}).status_code == 422
    assert patch(client, {"status": "PRESENT", "punch_in": ms(DAY, "09:30:00")}).status_code == 200
    assert patch(client, {"status": "LEAVE", "punch_in": ms(DAY, "09:30:00")}).status_code == 422
    assert patch(client, {"status": "ABSENT", "punch_out": None}).status_code == 422
    assert patch(client, {"punch_out": ms(DAY, "18:30:00"), "status": "ON_DUTY"}).status_code == 200
    assert patch(client, {"punch_in": ms("2026-07-07", "09:30:00")}).status_code == 422
    assert patch(client, {"punch_out": ms(DAY, "09:30:00")}).status_code == 422
    assert patch(client, {"punch_out": ms("2026-07-07", "09:30:01")}).status_code == 422


def test_regularization_validation_and_legacy_defaults(api):
    client, db = api
    add_employee(client)
    add_record(db, status="PRESENT", punch_in="09:30:00")
    assert patch(client, {"status": "WFH"}, code="UNKNOWN").status_code == 404
    assert patch(client, {"status": "WFH"}, day="2026-07-08").status_code == 404
    assert patch(client, {"status": "WFH"}, day="2026-02-30").status_code == 422
    assert patch(client, {"work_hours": 999, "half_day": True}).status_code == 422
    for body in ({"status": None}, {"status": "OTHER"}, {"punch_in": None}, {"punch_out": None}, {"punch_in": "1783312500000"}, {"punch_out": 1783312500}, {"reason": "x"}, {"regularized_by": ""}):
        assert patch(client, body).status_code == 422
    result = patch(client, {"status": "WFH", "late_minutes": 999})
    assert result.status_code == 200
    assert result.json()["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}
    assert result.json()["half_day"] is False
    assert db.attendance_logs.find_one({"emp_code": "EMP0001"})["_version"] == 1
    assert "emp_code_1_punch_in_-1" in db.attendance_logs.index_information()


def test_simultaneous_punch_out(api):
    client, db = api
    add_employee(client)
    add_record(db)
    body = {"emp_code": "EMP0001", "punched_at": ms(DAY, "18:30:00")}
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: client.post("/attendance/punch-out", json=body).status_code, range(8)))
    assert sorted(codes) == [200] + [409] * 7
    assert db.attendance_logs.find_one({"emp_code": "EMP0001"})["_version"] == 1


def pause_snapshot(monkeypatch, target):
    original = Collection.find_one
    barrier = Barrier(2)

    def find_one(self, filter=None, *args, **kwargs):
        result = original(self, filter, *args, **kwargs)
        if self.name == "attendance_logs" and target(filter):
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(Collection, "find_one", find_one)


def test_simultaneous_corrections_preserve_history(api, monkeypatch):
    client, db = api
    add_employee(client)
    add_record(db)
    pause_snapshot(monkeypatch, lambda q: q.get("date") == DAY)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(patch, client, {"status": "WFH"})
        b = pool.submit(patch, client, {"status": "ON_DUTY"})
        codes = sorted([a.result().status_code, b.result().status_code])
    assert codes == [200, 409]
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert len(stored["history"]) == 1 and stored["_version"] == 1


def test_punch_out_racing_with_correction(api, monkeypatch):
    client, db = api
    add_employee(client)
    add_record(db)
    pause_snapshot(monkeypatch, lambda q: q.get("date") == DAY or "punch_in" in q)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(lambda: client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(DAY, "18:30:00")}))
        b = pool.submit(patch, client, {"punch_in": ms(DAY, "09:15:00")})
        codes = sorted([a.result().status_code, b.result().status_code])
    assert codes == [200, 409]
    stored = db.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert stored["_version"] == 1
    if stored.get("punch_out") is None:
        assert stored["punch_in"] == instant(DAY, "09:15:00")
        assert len(stored["history"]) == 1
    else:
        assert stored["punch_in"] == instant(DAY, "09:30:00")
        assert stored["work_hours"] == 9.0
        assert stored.get("history", []) == []
