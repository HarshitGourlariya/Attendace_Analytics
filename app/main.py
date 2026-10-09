"""Attendance API built with FastAPI and MongoDB.

Run from candidate_kit with ``uvicorn app.main:app --port 8000``.
MongoDB stores instants in UTC; the API accepts/returns epoch milliseconds.
"""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Attendance Analytics", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="uq_employee_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_day")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="attendance_date_code")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1), ("punch_in", -1)], name="employee_latest_open_punch")
    db.attendance_logs.create_index([("date", 1), ("late_minutes", -1), ("emp_code", 1)], name="attendance_month_metrics")


def parse_date(value: str, field: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field} must be YYYY-MM-DD")


def parse_month(value: str) -> tuple[str, str, date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, value.split("-"))
    first = date(y, m, 1)
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return value, first.isoformat(), first, end


def trunc_second(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def from_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "invalid epoch milliseconds")


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def millis(value: Optional[datetime]):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def shift_boundary(attendance_date: str, shift: str, overnight_next=False) -> datetime:
    day = date.fromisoformat(attendance_date) + (timedelta(days=1) if overnight_next else timedelta())
    hour, minute = map(int, shift.split(":"))
    return datetime.combine(day, time(hour, minute), IST).astimezone(UTC)


def attendance_day(punch_in: datetime, employee: dict) -> str:
    local = punch_in.astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.strftime("%H:%M") < employee["shift_end"]:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: Optional[datetime], shift_start: str, day: str) -> int:
    if punch_in is None:
        return 0
    delta = int((trunc_second(punch_in) - shift_boundary(day, shift_start)).total_seconds())
    return delta // 60 if delta > 600 else 0


def compute_work_hours(punch_in: Optional[datetime], punch_out: Optional[datetime]):
    if punch_in is None or punch_out is None:
        return None
    hours = Decimal(str((punch_out - punch_in).total_seconds())) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: Optional[datetime], shift_end: str, day: str, overnight: bool) -> int:
    if punch_out is None:
        return 0
    elapsed = int((trunc_second(punch_out) - shift_boundary(day, shift_end, overnight)).total_seconds())
    minutes = elapsed // 60
    return minutes if minutes >= 30 else 0


def public_history(history):
    result = []
    for entry in history or []:
        item = dict(entry)
        item["at"] = millis(item.get("at"))
        item["changes"] = {k: {"from": millis(v["from"]) if k in ("punch_in", "punch_out") else v["from"],
                               "to": millis(v["to"]) if k in ("punch_in", "punch_out") else v["to"]}
                           for k, v in item.get("changes", {}).items()}
        result.append(item)
    return result


def public_attendance(doc):
    out = {k: doc.get(k) for k in ("emp_code", "date", "status", "work_hours", "late_minutes", "overtime_minutes")}
    out.update(punch_in=millis(doc.get("punch_in")), punch_out=millis(doc.get("punch_out")),
               half_day=bool(doc.get("half_day", False)), history=public_history(doc.get("history", [])))
    return out


def round_expr(expr, places=2):
    # MongoDB $round uses half-even; this expression rounds positive values half-up.
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expr, factor]}, 0.5]}}, factor]}


def month_match(start, end, emp_code=None):
    match = {"date": {"$gte": start, "$lte": end}}
    if emp_code:
        match["emp_code"] = emp_code
    return {"$match": match}


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_date(v, "joined_on")
        return v

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at must be omitted or an epoch-millisecond integer")
        return values


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, values):
        if isinstance(values, dict) and "punched_at" in values and values["punched_at"] is None:
            raise ValueError("punched_at must be omitted or an epoch-millisecond integer")
        return values


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|WFH|ON_DUTY|ABSENT|LEAVE)$")
    punch_in: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_nulls(cls, values):
        if isinstance(values, dict):
            for field_name in ("status", "punch_in", "punch_out"):
                if field_name in values and values[field_name] is None:
                    raise ValueError(f"{field_name} must be omitted or a valid value")
        return values


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return {**body.model_dump(), "created_at": millis(doc["created_at"])}


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = millis(item.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_day(ts, employee)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, employee["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return public_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    elapsed = (ts - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    overnight = employee["shift_end"] <= employee["shift_start"]
    hours = compute_work_hours(record["punch_in"], ts)
    update = {"punch_out": ts, "work_hours": hours,
              "overtime_minutes": compute_overtime(ts, employee["shift_end"], record["date"], overnight),
              "half_day": hours < 4.5}
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None}, {"$set": update}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return public_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from:
        parse_date(date_from, "date_from")
    if date_to:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from: query["date"]["$gte"] = date_from
        if date_to: query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [public_attendance(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old:
        raise HTTPException(404, "attendance record not found")
    status = body.status if body.status is not None else old["status"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have supplied punch times")
        pin = pout = None
    else:
        pin = from_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")
        pout = from_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")
        if pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_day(pin, employee) != date:
            raise HTTPException(422, "punch_in must belong to attendance date")
        if pout is not None and ((pout - pin).total_seconds() <= 0 or (pout - pin).total_seconds() > 86400):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    overnight = employee["shift_end"] <= employee["shift_start"]
    newvals = {"status": status, "punch_in": pin, "punch_out": pout,
               "late_minutes": compute_late_minutes(pin, employee["shift_start"], date),
               "work_hours": compute_work_hours(pin, pout),
               "overtime_minutes": compute_overtime(pout, employee["shift_end"], date, overnight),
               "half_day": bool(pout is not None and compute_work_hours(pin, pout) < 4.5)}
    tracked = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    changes = {}
    for key in tracked:
        before = old.get(key, False if key == "half_day" else 0 if key in ("late_minutes", "overtime_minutes") else None)
        if before != newvals[key]:
            changes[key] = {"from": before, "to": newvals[key]}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    compare = {"_id": old["_id"], "status": old["status"], "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    compare["history"] = old["history"] if "history" in old else {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        compare,
        {"$set": newvals, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry")
    return public_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    month, start, first, end = parse_month(month)
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    pipeline = [month_match(start, end.isoformat(), emp_code), {"$group": {"_id": None,
        "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
            {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
            {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]},
            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]
    stats = next(db.attendance_logs.aggregate(pipeline), {})
    joined_expr = {"$max": [{"$dateFromString": {"dateString": "$joined_on"}}, datetime.combine(first, time.min, UTC)]}
    days_expr = {"$dateDiff": {"startDate": joined_expr, "endDate": datetime.combine(end + timedelta(days=1), time.min, UTC), "unit": "day"}}
    working_expr = {"$size": {"$filter": {"input": {"$map": {"input": {"$range": [0, {"$max": [0, days_expr]}]},
        "as": "offset", "in": {"$add": [joined_expr, {"$multiply": ["$$offset", 86400000]}]}}},
        "as": "d", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]}}}}
    working_row = next(db.employees.aggregate([{"$match": {"emp_code": emp_code}}, {"$project": {"working_days": working_expr}}]), {})
    working = working_row.get("working_days", 0)
    present = stats.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": round_half(present),
            "leave_days": stats.get("leave_days", 0), "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get("total_late_minutes", 0), "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
            "attendance_pct": round_half(present / working * 100) if working else None}


def round_half(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal("1" if places == 0 else "0." + "0" * (places - 1) + "1"), rounding=ROUND_HALF_UP))


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    month, start, first, end = parse_month(month)
    match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None: match["department"] = department
    pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
        {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
         "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]},
             {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
             {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]},
             {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
         "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
         "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
         "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
         "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
         "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"},
         "present_days": 1, "avg_work_hours": {"$cond": [{"$eq": ["$avg_work_hours", None]}, None, round_expr("$avg_work_hours")]},
         "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    month, start, _, end = parse_month(month)
    pipeline = [month_match(start, end.isoformat()), {"$group": {"_id": "$emp_code",
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$set": {"name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}}]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query()):
    start_date, end_date = parse_date(from_, "from"), parse_date(to, "to")
    days = (end_date - start_date).days + 1
    if days <= 0 or days > 92:
        raise HTTPException(422, "date range must be ordered and at most 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    begin_dt = datetime.combine(start_date, time.min, UTC)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min, UTC)
    pipeline = [{"$match": {"department": department}}, {"$limit": 1}, {"$project": {"_id": 0, "date": {"$literal": begin_dt}}},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [begin_dt, end_dt]}}},
        {"$match": {"date": {"$lt": end_dt}}},
        {"$set": {"date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
            {"$count": "n"}], "as": "headcount_arr"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"}, {"$match": {"employee.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "daily"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_arr.n", 0]}, 0]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, round_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, round_expr("$moving_avg_7d", 4)]}}},
        {"$sort": {"date": 1}}]
    return {"department": department, "items": list(db.employees.aggregate(pipeline, allowDiskUse=True, hint="department_joined"))}


def explain_for(endpoint: str, params: dict):
    if endpoint == "attendance_list":
        q = {}
        for k in ("emp_code", "status"):
            if params.get(k) is not None: q[k] = params[k]
        date_filter = {}
        if params.get("date_from"): date_filter["$gte"] = params["date_from"]
        if params.get("date_to"): date_filter["$lte"] = params["date_to"]
        if date_filter: q["date"] = date_filter
        coll, query, sort = "attendance_logs", q, [("date", -1), ("emp_code", 1)]
        explanation = db.command("explain", {"find": coll, "filter": query, "sort": dict(sort),
            "skip": (params.get("page", 1) - 1) * params.get("page_size", 20), "limit": params.get("page_size", 20)}, verbosity="executionStats")
    else:
        if endpoint == "employee_monthly":
            _, start, _, end = parse_month(params["month"])
            coll = "attendance_logs"
            pipeline = [month_match(start, end.isoformat(), params["emp_code"]), {"$group": {"_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]
        elif endpoint == "department_summary":
            _, start, _, end = parse_month(params["month"])
            coll = "employees"
            match = {"joined_on": {"$lte": end.isoformat()}}
            if params.get("department") is not None: match["department"] = params["department"]
            pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end.isoformat()]}]}}}], "as": "logs"}},
                {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
                {"$group": {"_id": "$department", "headcount": {"$addToSet": "$emp_code"},
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, 6]}]},
                        {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0]}},
                    "avg_work_hours": {"$avg": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
                {"$project": {"_id": 0, "department": "$_id", "headcount": {"$size": "$headcount"}, "present_days": 1,
                    "avg_work_hours": {"$cond": [{"$eq": ["$avg_work_hours", None]}, None, round_expr("$avg_work_hours")]},
                    "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}, {"$sort": {"department": 1}}]
        elif endpoint == "late_leaderboard":
            _, start, _, end = parse_month(params["month"])
            coll = "attendance_logs"
            pipeline = [month_match(start, end.isoformat()), {"$group": {"_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
            if params.get("department") is not None: pipeline.append({"$match": {"employee.department": params["department"]}})
            pipeline += [{"$set": {"name": "$employee.name", "department": "$employee.department"}},
                {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
                {"$match": {"rank": {"$lte": params.get("limit", 10)}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}},
                {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}}]
        else:
            coll = "employees"
            start_date = parse_date(params["from"], "from")
            end_date = parse_date(params["to"], "to")
            begin_dt = datetime.combine(start_date, time.min, UTC)
            end_dt = datetime.combine(end_date + timedelta(days=1), time.min, UTC)
            pipeline = [{"$match": {"department": params["department"]}}, {"$limit": 1}, {"$project": {"_id": 0, "date": {"$literal": begin_dt}}},
                {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [begin_dt, end_dt]}}},
                {"$match": {"date": {"$lt": end_dt}}}, {"$set": {"date": {"$dateToString": {"date": "$date", "format": "%Y-%m-%d"}}}},
                {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", params["department"]]}, {"$lte": ["$joined_on", "$$day"]}]}}}, {"$count": "n"}], "as": "headcount_arr"}},
                {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                    {"$unwind": "$employee"}, {"$match": {"employee.department": params["department"]}},
                    {"$group": {"_id": None, "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}], "as": "daily"}},
                {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_arr.n", 0]}, 0]},
                    "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]}, "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
                    "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}]}}},
                {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, round_expr({"$divide": ["$present_count", "$headcount"]}, 4), None]}}},
                {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
                {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
                    "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, round_expr("$moving_avg_7d", 4)]}}}, {"$sort": {"date": 1}}]
        command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
        if endpoint == "department_trend":
            command["hint"] = "department_joined"
        explanation = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": coll, "explain": json_util.loads(json_util.dumps(explanation))}


@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
                  department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
                  date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                  page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                  from_: Optional[str] = Query(default=None, alias="from"), to: Optional[str] = None):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed: raise HTTPException(422, "unknown endpoint")
    params = locals()
    needed = {"employee_monthly": ["emp_code", "month"], "department_summary": ["month"],
              "late_leaderboard": ["month"], "department_trend": ["department", "from_", "to"]}.get(endpoint, [])
    if any(not params.get(k) for k in needed): raise HTTPException(422, "missing required query parameter")
    if endpoint == "attendance_list":
        if date_from: parse_date(date_from, "date_from")
        if date_to: parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to: raise HTTPException(422, "invalid date range")
        if status is not None and status not in STATUSES: raise HTTPException(422, "invalid status")
    if month: parse_month(month)
    if endpoint == "department_trend":
        start_date = parse_date(from_, "from")
        end_date = parse_date(to, "to")
        if end_date < start_date or (end_date - start_date).days + 1 > 92:
            raise HTTPException(422, "date range must be ordered and at most 92 days")
    return explain_for(endpoint, {**params, "from": from_})
