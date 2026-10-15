from datetime import datetime, timezone
from unittest.mock import MagicMock
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ConnectionFailure, OperationFailure
from pymongo import MongoClient

import app.main as main
from test_attendance_core import employee


@pytest.mark.parametrize("failed_call,error", [
    (1, ConnectionFailure("database unavailable")),
    (5, ConnectionFailure("connection lost midway")),
    (8, OperationFailure("index conflict")),
])
def test_index_failure_aborts_startup_and_closes_client(monkeypatch, failed_call, error):
    calls = []
    fake_client = MagicMock()
    fake_db = MagicMock()
    fake_client.__getitem__.return_value = fake_db

    def create_index(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == failed_call:
            raise error

    fake_db.employees.create_index.side_effect = create_index
    fake_db.attendance_logs.create_index.side_effect = create_index
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost:27017")
    monkeypatch.setenv("MONGO_DB", "disposable")
    monkeypatch.setattr(main, "MongoClient", lambda *args, **kwargs: fake_client)
    with pytest.raises(type(error), match=str(error)):
        with TestClient(main.app):
            pass
    assert len(calls) == failed_call
    fake_client.close.assert_called_once_with()

    fake_db.command.return_value = {"ok": 1}
    assert fake_db.command("ping") == {"ok": 1}
    assert len(calls) == failed_call


def test_real_indexes_and_repeat_startup(api, monkeypatch):
    _, db = api
    expected_employees = {
        "emp_code_1": True,
        "department_1_emp_code_1": False,
        "joined_on_1_department_1": False,
        "department_1_joined_on_1": False,
    }
    expected_attendance = {
        "emp_code_1_date_1": True,
        "emp_code_1_punch_in_-1": False,
        "date_-1_emp_code_1": False,
        "status_1_date_-1_emp_code_1": False,
    }

    def assert_indexes():
        for collection, expected in ((db.employees, expected_employees), (db.attendance_logs, expected_attendance)):
            actual = collection.index_information()
            assert set(actual) == {"_id_", *expected}
            for name, unique in expected.items():
                assert bool(actual[name].get("unique", False)) is unique

    assert_indexes()
    with TestClient(main.app) as client:
        assert client.get("/health").status_code == 200
        assert_indexes()


def test_duplicate_seeded_employees_prevent_startup(mongo_server, monkeypatch):
    name = "hrone_test_" + uuid.uuid4().hex
    client = MongoClient(mongo_server)
    try:
        db = client[name]
        db.employees.insert_many([{"emp_code": "EMP0001"}, {"emp_code": "EMP0001"}])
        monkeypatch.setenv("MONGO_URI", mongo_server)
        monkeypatch.setenv("MONGO_DB", name)
        with pytest.raises(OperationFailure):
            with TestClient(main.app):
                pass
        assert db.employees.count_documents({"emp_code": "EMP0001"}) == 2
    finally:
        client.drop_database(name)
        client.close()


def test_employee_created_at_server_clock_seconds(api, monkeypatch):
    client, db = api
    fixed = datetime(2026, 7, 6, 3, 58, 0, 987654, tzinfo=timezone.utc)
    real_datetime = datetime

    class ControlledDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == timezone.utc
            return fixed

    monkeypatch.setattr(main, "datetime", ControlledDatetime)
    response = client.post("/employees", json={**employee(), "created_at": 1})
    assert response.status_code == 201, response.text
    posted = response.json()
    expected_ms = 1783310280000
    assert posted["created_at"] == expected_ms
    assert isinstance(posted["created_at"], int)
    assert posted["created_at"] % 1000 == 0
    stored = db.employees.find_one({"emp_code": "EMP0001"})["created_at"]
    assert stored.tzinfo is not None and stored.utcoffset().total_seconds() == 0
    assert stored.microsecond == 0
    listed = client.get("/employees").json()["items"][0]
    assert listed["created_at"] == posted["created_at"]
