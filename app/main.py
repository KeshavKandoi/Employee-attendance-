from __future__ import annotations

import os
import json
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
EpochMillis = Annotated[StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]
ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


def validate_calendar_date(value: str) -> str:
    if len(value) != 10 or value[4] != "-" or value[7] != "-" or not (value[:4] + value[5:7] + value[8:]).isascii() or not (value[:4] + value[5:7] + value[8:]).isdigit():
        raise ValueError("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a real YYYY-MM-DD calendar date") from exc
    return value


CalendarDate = Annotated[str, AfterValidator(validate_calendar_date)]


def validate_month(value: str) -> str:
    try:
        date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise ValueError("month must be a real YYYY-MM calendar month") from exc
    return value


CalendarMonth = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), AfterValidator(validate_month)]


def epoch_ms_to_utc(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)


def utc_to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def truncate_seconds(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = truncate_seconds(punch_in).astimezone(IST)
    day = local.date()
    if shift_end <= shift_start and local.time().replace(tzinfo=None) < time.fromisoformat(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_instants(attendance_day: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    day = date.fromisoformat(attendance_day)
    start = datetime.combine(day, time.fromisoformat(shift_start), IST)
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    end = datetime.combine(end_day, time.fromisoformat(shift_end), IST)
    return start.astimezone(UTC), end.astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: str) -> int:
    start, _ = shift_instants(attendance_day, shift_start, shift_start)
    seconds = int((truncate_seconds(punch_in) - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def round_half_up(value: Decimal, places: int = 2) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((truncate_seconds(punch_out) - truncate_seconds(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600))


def compute_overtime(punch_out: datetime, shift_end: str, attendance_day: str, shift_start: str) -> int:
    _, end = shift_instants(attendance_day, shift_start, shift_end)
    minutes = int((truncate_seconds(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: float) -> bool:
    return Decimal(str(work_hours)) < Decimal("4.50")


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: CalendarDate

    @model_validator(mode="after")
    def distinct_shift_times(self) -> EmployeeIn:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None


class RegularizeIn(BaseModel):
    status: AttendanceStatus = None
    punch_in: EpochMillis = None
    punch_out: EpochMillis = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


def version_filter(doc: dict) -> dict:
    return {"_version": doc["_version"]} if "_version" in doc else {"_version": {"$exists": False}}


def invalid_request(message: str, field: str, value: object = None) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=[{"type": "value_error", "loc": ["body", field], "msg": message, "input": value}],
    )


def derived_values(status: str, punch_in: datetime | None, punch_out: datetime | None,
                   employee: dict, attendance_day: str) -> dict:
    if status in ("ABSENT", "LEAVE"):
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    assert punch_in is not None
    late = compute_late_minutes(punch_in, employee["shift_start"], attendance_day)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    hours = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": hours, "late_minutes": late,
        "overtime_minutes": compute_overtime(
            punch_out, employee["shift_end"], attendance_day, employee["shift_start"]
        ),
        "half_day": compute_half_day(hours),
    }


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": utc_to_epoch_ms(doc["created_at"]),
    }


def serialize_attendance(doc: dict) -> dict:
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            changes[field] = {
                side: utc_to_epoch_ms(value) if field in ("punch_in", "punch_out") and isinstance(value, datetime) else value
                for side, value in change.items()
            }
        history.append({"at": utc_to_epoch_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes})
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": utc_to_epoch_ms(doc["punch_in"]) if doc.get("punch_in") is not None else None,
        "punch_out": utc_to_epoch_ms(doc["punch_out"]) if doc.get("punch_out") is not None else None,
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": history,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = MongoClient(
        os.environ["MONGO_URI"],
        tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000,
    )
    try:
        db = client[os.environ["MONGO_DB"]]
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
        app.state.mongo_client = client
        app.state.db = db
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan, docs_url=None)


@app.get("/docs", include_in_schema=False)
def swagger_docs():
    page = get_swagger_ui_html(openapi_url=app.openapi_url, title=f"{app.title} - Swagger UI")
    styles = """<style>
    .swagger-ui .info .title > span,
    .swagger-ui .info .title small,
    .swagger-ui .info .main > a { display: none; }
    </style>"""
    return HTMLResponse(page.body.decode("utf-8").replace("</head>", styles + "</head>"))


@app.get("/health")
def health():
    try:
        app.state.db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_seconds(datetime.now(UTC))
    try:
        app.state.db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department is not None else {}
    collection = app.state.db.employees
    total = collection.count_documents(query)
    cursor = collection.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [serialize_employee(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = app.state.db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    instant = truncate_seconds(epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    day = attendance_date(instant, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": instant, "punch_out": None, "work_hours": None,
        "late_minutes": compute_late_minutes(instant, employee["shift_start"], day),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        app.state.db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = app.state.db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    instant = truncate_seconds(epoch_ms_to_utc(body.punched_at) if body.punched_at is not None else datetime.now(UTC))
    collection = app.state.db.attendance_logs
    record = collection.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": instant}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no eligible punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")
    start = record["punch_in"]
    elapsed = instant - start
    if elapsed <= timedelta(0) or elapsed > timedelta(hours=24):
        raise invalid_request("punched_at must be after punch_in and within 24 hours", "punched_at", body.punched_at)
    hours = compute_work_hours(start, instant)
    update = {
        "$set": {
            "punch_out": instant,
            "work_hours": hours,
            "overtime_minutes": compute_overtime(
                instant, employee["shift_end"], record["date"], employee["shift_start"]
            ),
            "half_day": compute_half_day(hours),
        },
        "$inc": {"_version": 1},
    }
    result = collection.find_one_and_update(
        {"_id": record["_id"], **version_filter(record), "punch_out": None},
        update,
        return_document=True,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="record changed concurrently")
    return serialize_attendance(result)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: CalendarDate, body: RegularizeIn):
    employee = app.state.db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    collection = app.state.db.attendance_logs
    record = collection.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else record["status"]
    if final_status in ("ABSENT", "LEAVE"):
        if "status" in supplied and ("punch_in" in supplied or "punch_out" in supplied):
            raise invalid_request("punch times cannot be supplied with ABSENT or LEAVE", "status", final_status)
        if "punch_in" in supplied or "punch_out" in supplied:
            raise invalid_request("punch times require a presence status", "status", final_status)
        final_in = final_out = None
    else:
        final_in = truncate_seconds(epoch_ms_to_utc(body.punch_in)) if "punch_in" in supplied else record.get("punch_in")
        final_out = truncate_seconds(epoch_ms_to_utc(body.punch_out)) if "punch_out" in supplied else record.get("punch_out")
        if final_in is None:
            raise invalid_request("a presence status requires punch_in", "punch_in", None)
        if attendance_date(final_in, employee["shift_start"], employee["shift_end"]) != date:
            raise invalid_request("punch_in must remain on the attendance date", "punch_in", body.punch_in)
        if final_out is not None and (final_out <= final_in or final_out - final_in > timedelta(hours=24)):
            raise invalid_request("punch_out must be after punch_in and within 24 hours", "punch_out", body.punch_out)

    final = {"status": final_status, "punch_in": final_in, "punch_out": final_out}
    final.update(derived_values(final_status, final_in, final_out, employee, date))
    defaults = {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {
        field: {"from": record.get(field, defaults.get(field)), "to": value}
        for field, value in final.items()
        if record.get(field, defaults.get(field)) != value
    }
    if not changes:
        raise invalid_request("correction changes no attendance fields", "status", body.status)
    entry = {
        "at": truncate_seconds(datetime.now(UTC)), "by": body.regularized_by,
        "reason": body.reason, "changes": changes,
    }
    result = collection.find_one_and_update(
        {"_id": record["_id"], **version_filter(record)},
        {"$set": final, "$push": {"history": entry}, "$inc": {"_version": 1}},
        return_document=True,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="record changed concurrently")
    return serialize_attendance(result)


def attendance_list_operation(
    emp_code: str | None, date_from: str | None, date_to: str | None,
    status: str | None, page: int, page_size: int,
) -> dict:
    """The find command used by both the list route and executionStats explain."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail=[{"type": "value_error", "loc": ["query", "date_from"], "msg": "date_from must be on or before date_to", "input": date_from}])
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    return {
        "filter": query, "sort": {"date": DESCENDING, "emp_code": ASCENDING},
        "skip": (page - 1) * page_size, "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: CalendarDate | None = None,
    date_to: CalendarDate | None = None,
    status: AttendanceStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    operation = attendance_list_operation(emp_code, date_from, date_to, status, page, page_size)
    collection = app.state.db.attendance_logs
    total = collection.count_documents(operation["filter"])
    cursor = collection.find(operation["filter"]).sort(list(operation["sort"].items())).skip(operation["skip"]).limit(operation["limit"])
    return {"items": [serialize_attendance(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ANALYTICS_OPTIONS = {"allowDiskUse": True}
DECIMAL_ZERO = Decimal128("0")
DECIMAL_HALF = Decimal128("0.5")
DECIMAL_ONE = Decimal128("1")


def month_bounds(month: str) -> tuple[str, str, datetime, int]:
    year, number = map(int, month.split("-"))
    days = monthrange(year, number)[1]
    return f"{month}-01", f"{month}-{days:02d}", datetime(year, number, 1, tzinfo=UTC), days


def mongo_half_up(value: dict | str, places: int = 2) -> dict:
    """Round a nonnegative aggregate with Decimal128, then emit a JSON number.

    MongoDB's $round is half-even; this uses floor(value * scale + 0.5) / scale.
    The contract's rates and work-hour averages are nonnegative.
    """
    scale = Decimal128(str(10 ** places))
    return {
        "$toDouble": {
            "$let": {
                "vars": {"number": {"$toDecimal": value}},
                "in": {
                    "$divide": [
                        {"$floor": {"$add": [{"$multiply": ["$$number", scale]}, DECIMAL_HALF]}},
                        scale,
                    ]
                },
            }
        }
    }


def working_days_expression(month_start: datetime, days: int) -> dict:
    """Count Monday–Friday dates no earlier than joined_on inside MongoDB."""
    return {
        "$size": {
            "$filter": {
                "input": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "offset",
                        "in": {"$dateAdd": {"startDate": month_start, "unit": "day", "amount": "$$offset"}},
                    }
                },
                "as": "day",
                "cond": {
                    "$and": [
                        {"$in": [{"$isoDayOfWeek": "$$day"}, [1, 2, 3, 4, 5]]},
                        {"$gte": [{"$dateToString": {"format": "%Y-%m-%d", "date": "$$day"}}, "$joined_on"]},
                    ]
                },
            }
        }
    }


def log_metrics_pipeline(month_start: str, month_end: str) -> list[dict]:
    """One row of totals for an employee; used by both employee and department APIs."""
    presence = {"$in": ["$status", PRESENCE_STATUSES]}
    weekday = {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [1, 2, 3, 4, 5]]}
    late = {"$ifNull": ["$late_minutes", 0]}
    eligible_hours = {"$and": [presence, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [presence, weekday]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, DECIMAL_HALF, DECIMAL_ONE]},
                DECIMAL_ZERO,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": late},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "hours_sum": {"$sum": {"$cond": [eligible_hours, {"$toDecimal": {"$ifNull": ["$work_hours", 0]}}, DECIMAL_ZERO]}},
            "hours_count": {"$sum": {"$cond": [eligible_hours, 1, 0]}},
        }},
    ]


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end, start_instant, days = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": log_metrics_pipeline(start, end), "as": "metrics",
        }},
        {"$set": {
            "metrics": {"$ifNull": [{"$arrayElemAt": ["$metrics", 0]}, {}]},
            "working_days": working_days_expression(start_instant, days),
        }},
        {"$project": {
            "_id": 0, "emp_code": 1,
            "month": {"$literal": month},
            "working_days": 1,
            "present_days": {"$toDouble": {"$ifNull": ["$metrics.present_days", DECIMAL_ZERO]}},
            "leave_days": {"$ifNull": ["$metrics.leave_days", 0]},
            "late_count": {"$ifNull": ["$metrics.late_count", 0]},
            "total_late_minutes": {"$ifNull": ["$metrics.total_late_minutes", 0]},
            "total_overtime_minutes": {"$ifNull": ["$metrics.total_overtime_minutes", 0]},
            "attendance_pct": {"$cond": [
                {"$gt": ["$working_days", 0]},
                mongo_half_up({"$multiply": [
                    {"$divide": [{"$ifNull": ["$metrics.present_days", DECIMAL_ZERO]}, {"$toDecimal": "$working_days"}]},
                    Decimal128("100"),
                ]}),
                None,
            ]},
        }},
    ]


def department_summary_pipeline(month: str, department: str | None) -> list[dict]:
    start, end, _, _ = month_bounds(month)
    match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": log_metrics_pipeline(start, end), "as": "metrics",
        }},
        {"$set": {"metrics": {"$ifNull": [{"$arrayElemAt": ["$metrics", 0]}, {}]}}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$metrics.present_days", DECIMAL_ZERO]}},
            "hours_sum": {"$sum": {"$ifNull": ["$metrics.hours_sum", DECIMAL_ZERO]}},
            "hours_count": {"$sum": {"$ifNull": ["$metrics.hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$metrics.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$metrics.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$metrics.leave_days", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$metrics.on_duty_count", 0]}},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": {"$toDouble": "$present_days"},
            "avg_work_hours": {"$cond": [
                {"$gt": ["$hours_count", 0]},
                mongo_half_up({"$divide": ["$hours_sum", {"$toDecimal": "$hours_count"}]}),
                None,
            ]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: str | None) -> list[dict]:
    start, end, _, _ = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "late_count": 1,
        }},
    ])
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: CalendarMonth):
    rows = list(app.state.db.employees.aggregate(employee_monthly_pipeline(emp_code, month), **ANALYTICS_OPTIONS))
    if not rows:
        raise HTTPException(status_code=404, detail="employee not found")
    return rows[0]


@app.get("/analytics/departments/summary")
def department_summary(month: CalendarMonth, department: str | None = None):
    rows = list(app.state.db.employees.aggregate(department_summary_pipeline(month, department), **ANALYTICS_OPTIONS))
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: CalendarMonth, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: str | None = None):
    rows = list(app.state.db.attendance_logs.aggregate(
        late_leaderboard_pipeline(month, limit, department), **ANALYTICS_OPTIONS
    ))
    return {"month": month, "items": rows}


def validate_trend_range(from_date: str, to_date: str) -> int:
    first, last = date.fromisoformat(from_date), date.fromisoformat(to_date)
    days = (last - first).days + 1
    if days < 1:
        raise HTTPException(status_code=422, detail=[{
            "type": "value_error", "loc": ["query", "to"],
            "msg": "to must be on or after from", "input": to_date,
        }])
    if days > 92:
        raise HTTPException(status_code=422, detail=[{
            "type": "value_error", "loc": ["query", "to"],
            "msg": "range must contain at most 92 days", "input": to_date,
        }])
    return days


def department_trend_pipeline(department: str, from_date: str, to_date: str) -> list[dict]:
    days = validate_trend_range(from_date, to_date)
    start = datetime.combine(date.fromisoformat(from_date), time.min, UTC)
    presence = {"$in": ["$status", PRESENCE_STATUSES]}
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "days": {"$map": {
            "input": {"$range": [0, days]}, "as": "offset",
            "in": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$offset"}},
        }}}},
        {"$unwind": "$days"},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$days"}}}},
        {"$lookup": {
            "from": "employees", "let": {"day": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                {"$count": "headcount"},
            ],
            "as": "headcount_docs",
        }},
        {"$lookup": {
            "from": "attendance_logs", "localField": "date", "foreignField": "date",
            "pipeline": [
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$group": {"_id": None,
                    "present_count": {"$sum": {"$cond": [
                        presence,
                        {"$cond": [{"$ifNull": ["$half_day", False]}, DECIMAL_HALF, DECIMAL_ONE]},
                        DECIMAL_ZERO,
                    ]}},
                    "late_count": {"$sum": {"$cond": [
                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0,
                    ]}},
                }},
            ],
            "as": "daily_docs",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_docs.headcount", 0]}, 0]},
            "present_decimal": {"$ifNull": [{"$arrayElemAt": ["$daily_docs.present_count", 0]}, DECIMAL_ZERO]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily_docs.late_count", 0]}, 0]},
            "is_working_day": {"$in": [{"$isoDayOfWeek": "$days"}, [1, 2, 3, 4, 5]]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            mongo_half_up({"$divide": ["$present_decimal", {"$toDecimal": "$headcount"}]}, 4),
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_raw": {
                "$avg": {"$toDecimal": "$attendance_rate"},
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": {"$toDouble": "$present_decimal"}, "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [
                {"$ne": ["$moving_raw", None]}, mongo_half_up("$moving_raw", 4), None,
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[CalendarDate, Query(alias="from")],
    to_date: Annotated[CalendarDate, Query(alias="to")],
):
    pipeline = department_trend_pipeline(department, from_date, to_date)
    rows = list(app.state.db.employees.aggregate(pipeline, **ANALYTICS_OPTIONS))
    if not rows:
        raise HTTPException(status_code=404, detail="department not found")
    return {"department": department, "items": rows}


def missing_query(name: str) -> HTTPException:
    return HTTPException(status_code=422, detail=[{
        "type": "missing", "loc": ["query", name], "msg": "Field required", "input": None,
    }])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None,
    month: CalendarMonth | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: CalendarDate | None = None,
    date_to: CalendarDate | None = None,
    status: AttendanceStatus | None = None,
    from_date: Annotated[CalendarDate | None, Query(alias="from")] = None,
    to_date: Annotated[CalendarDate | None, Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        operation = attendance_list_operation(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, **operation}
    else:
        if endpoint in ("employee_monthly", "department_summary", "late_leaderboard") and month is None:
            raise missing_query("month")
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise missing_query("emp_code")
            collection = "employees"
            pipeline = employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = late_leaderboard_pipeline(month, limit, department)
        else:
            if department is None:
                raise missing_query("department")
            if from_date is None:
                raise missing_query("from")
            if to_date is None:
                raise missing_query("to")
            collection = "employees"
            pipeline = department_trend_pipeline(department, from_date, to_date)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, **ANALYTICS_OPTIONS}
    plan = app.state.db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(plan))}
