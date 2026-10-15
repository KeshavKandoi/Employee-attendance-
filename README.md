# HROne Employee Attendance & Analytics API

The API implements the endpoints in `openapi.yaml`: health, employees, attendance, monthly and department analytics, late leaderboard, department trend, and execution-plan explain.

## Run

Use Python 3.11+ and MongoDB 6.0+ (verified here with Python 3.11.14 and MongoDB 7.0.14). From this directory:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in the environment or local `.env`. Environment variables take precedence. Startup creates every required index before serving traffic; any index failure aborts startup. `GET /health` pings MongoDB and returns 503 during an outage after startup. Punch-out and regularization use a per-record version check and return 409 on a conflicting update. Regularization appends audit history atomically. The optional sample seed script **deletes existing collection contents**; use it only with a verified disposable database. It does not run at startup.

## Analytics choices

The monthly and department pipelines start from `employees`, then look up indexed attendance records. This keeps employees with no logs in headcount and gives them zero totals. Department average hours divide the sum of eligible record hours by the number of eligible records, so employees with different numbers of logs have the correct weight. The leaderboard groups logs, joins existing employees, applies any department filter, and uses MongoDB's competition `$rank` before the rank cutoff; ties at the cutoff remain in the result.

The pipelines convert stored work hours to Decimal128 for summation. Reported averages and rates use `floor(value × scale + 0.5) / scale` in Decimal128, then convert to JSON numbers. This gives half-up rounding for the contract's nonnegative values; MongoDB `$round` uses half-even. The trend pipeline generates its bounded date spine inside MongoDB from one department employee. It looks up daily headcount and attendance totals, then applies a seven-row window average to the rounded, nullable daily rates. The explain endpoint calls the same query and pipeline builders with the same options as the public routes, requesting `executionStats`; BSON values in the raw plan use Extended JSON where ordinary JSON has no equivalent.

The index choices are tied to the operations: unique `employees.emp_code` and unique `attendance_logs.(emp_code, date)` enforce identities; `employees.(department, emp_code)` supports filtered employee sorting; `employees.(joined_on, department)` supports unfiltered monthly headcount; `employees.(department, joined_on)` supports daily department headcount; `attendance_logs.(emp_code, punch_in)` supports latest eligible punch-out; `attendance_logs.(date, emp_code)` supports the default attendance list, date joins, and monthly leaderboard; and `attendance_logs.(status, date, emp_code)` supports status-filtered lists. The unique employee/date index also supports employee-specific attendance lookups. No index is created solely for explain.

## Test

```bash
pytest -q
```

The integration and concurrency tests start an isolated disposable `mongod` process on localhost and create uniquely named test databases, then remove them. They skip if `mongod` is unavailable. To test against an existing disposable MongoDB instance, set `HRONE_TEST_MONGO_URI`; the tests still create and drop only their own uniquely named databases. The pure helper tests do not need MongoDB. A skip is not database verification.

For an optional large-data check, run `pytest -s -q tests/benchmark_phase4.py`. It creates 100,000 synthetic attendance records in another disposable database and prints startup, query, and `executionStats` timings. It never runs `sample_seed.py`.

## Measured performance

Current MongoDB 7.0.14 run, with 500 employees and 100,000 attendance records: startup including index creation 0.828 seconds. HTTP timings in seconds: attendance list 0.020, filtered page 0.006, employee monthly 0.005, department summary 0.073, late leaderboard 0.029, 92-day department trend 1.482. The five `executionStats` requests took 0.005, 0.005, 0.080, 0.031, and 1.457 seconds in the order listed by the benchmark. Each showed `IXSCAN`, zero reported outer collection scans, and zero lookup collection scans. Full regression suite: 31 passed, none skipped. A separate Uvicorn process returned 200 from `/health` against a disposable MongoDB 7 database.

Historical MongoDB 8.0.10 measurements before Phase 5: startup 0.955 seconds and 92-day trend 2.513 seconds on the same dataset size. These local timings are environment dependent. The grader's hidden dataset and tests remain unverified.
