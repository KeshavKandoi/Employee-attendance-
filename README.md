# HROne Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee records, attendance, corrections, and attendance analytics. The HTTP contract and business rules are in [`openapi.yaml`](openapi.yaml); stored document shapes are in [`DATA_MODEL.md`](DATA_MODEL.md).

## Capabilities

- Create and list employees; record punch-in and punch-out for day or overnight shifts.
- Correct attendance with recalculated totals and append-only audit history.
- List attendance with filters and pagination; report monthly employee metrics, department summaries, late rankings, and daily department trends.
- Inspect execution plans for the five list or analytics operations through the explain endpoint.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Check MongoDB readiness |
| POST, GET | `/employees` | Create or list employees |
| POST | `/attendance/punch-in` | Open an attendance record |
| POST | `/attendance/punch-out` | Close the latest eligible record |
| GET | `/attendance` | Filter and page attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Correct one record |
| GET | `/analytics/employees/{emp_code}/monthly` | Employee monthly metrics |
| GET | `/analytics/departments/summary` | Department monthly metrics |
| GET | `/analytics/leaderboard/late` | Late ranking |
| GET | `/analytics/departments/{department}/trend` | Daily trend |
| GET | `/admin/explain/{endpoint}` | Query execution statistics |

## Requirements and setup

Use Python 3.11+ and MongoDB 6.0+ (local or Atlas). Install dependencies from the repository root:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `MONGO_URI` and `MONGO_DB` in `.env` or the process environment. Process environment variables take precedence. For local MongoDB, use `MONGO_URI=mongodb://localhost:27017` and a database name such as `MONGO_DB=attendance_db`. For Atlas, use a URI such as `MONGO_URI=mongodb+srv://<user>:<password>@<cluster-host>/?retryWrites=true&w=majority` and set `MONGO_DB=<database-name>`. Replace placeholders locally and keep credentials out of Git. Ensure the database is reachable and the configured user can create indexes and read and write both collections.

Start the service with:

```bash
uvicorn app.main:app --port 8000
```

Swagger UI is at [http://localhost:8000/docs](http://localhost:8000/docs). Startup creates all required indexes before accepting requests; a failed index build aborts startup. Unique indexes on employee code and `(emp_code, date)` enforce one employee and one attendance record per employee per attendance day.

## Try the API

Create an employee, then punch in and punch out later. These commands use the current server time and therefore need no `punched_at` field:

```bash
curl -X POST http://localhost:8000/employees -H 'Content-Type: application/json' -d '{"emp_code":"EMP9005","name":"Asha Rao","email":"asha@example.com","department":"Engineering","joined_on":"2026-07-01"}'
curl -X POST http://localhost:8000/attendance/punch-in -H 'Content-Type: application/json' -d '{"emp_code":"EMP9005","status":"PRESENT"}'
curl -X POST http://localhost:8000/attendance/punch-out -H 'Content-Type: application/json' -d '{"emp_code":"EMP9005"}'
```

Punch-out must be later than punch-in and within 24 hours. Explicit instant fields, including `punched_at` and correction times, are **integer epoch milliseconds**; ISO date strings are invalid. Omitted punch times use the UTC server clock truncated to whole seconds. Attendance dates follow the contract's IST rules, including overnight shifts. Date-only fields use `YYYY-MM-DD`; month parameters use `YYYY-MM`.

Corrections append an audit-history entry and recalculate derived fields. Analytics run as MongoDB aggregation pipelines over stored attendance metrics. Department headcount includes employees with no logs.

## Sample data

`sample_seed.py` **deletes existing documents from both collections** in the configured database before loading the included examples. Use a verified disposable database; this step is optional and never runs at startup.

```bash
MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_sample python sample_seed.py
```

For Atlas, replace the local URI with your configured Atlas URI and verify the database name before running the command.

## Tests and benchmark

```bash
pytest -q
pytest -q tests/test_attendance_core.py tests/test_attendance_corrections.py tests/test_analytics.py tests/test_trends_explain.py tests/test_startup_indexes.py
pytest -s -q tests/benchmark_large_dataset.py
```

Integration tests start an isolated local `mongod` and use uniquely named disposable databases. They skip when `mongod` is unavailable. To use a separately managed test server, set `HRONE_TEST_MONGO_URI` to its URI; tests still create and drop only their own named databases. The optional benchmark loads 500 synthetic employees and 100,000 attendance records into a disposable database, then prints route timings and five `executionStats` summaries. It does not run during the default suite.

The service does not provide authentication or access control; use it only in an appropriately isolated environment. Local performance varies by database and machine, and the supplied sample data does not cover every contract case.
