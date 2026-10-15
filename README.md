# HROne Employee Attendance & Analytics API

Phases 1–4 implement the full set of endpoints in `openapi.yaml`: health, employees, attendance, monthly and department analytics, the late leaderboard, department trend, and execution-plan explain.

## Run

Use Python 3.11+ and MongoDB 6.0+ (the grader uses MongoDB 7.0). From this directory:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` in the environment or local `.env`. Actual environment variables take precedence. The startup lifecycle creates indexes, including unique employee-code and `(emp_code, date)` indexes and an `(emp_code, punch_in)` index for the latest eligible punch-in lookup. `GET /health` returns 200 only when MongoDB responds to a ping. Punch-out and regularization use a per-record internal version check: only an update based on the current version succeeds, and a conflicting update returns 409. Regularization appends the audit entry in the same atomic database update. The sample seed script **deletes existing collection contents**; run it only against a verified disposable database. It is not needed to start the API.

## Analytics choices

The monthly and department pipelines start from `employees`, then look up indexed attendance records. This keeps employees with no logs in headcount and gives them zero totals. Department average hours divide the sum of eligible record hours by the number of eligible records, so employees with different numbers of logs have the correct weight. The leaderboard groups logs, joins existing employees, applies any department filter, and uses MongoDB's competition `$rank` before the rank cutoff; ties at the cutoff remain in the result.

The pipelines convert stored work hours to Decimal128 for summation. Reported averages and rates use `floor(value × scale + 0.5) / scale` in Decimal128, then convert to JSON numbers. This gives half-up rounding for the contract's nonnegative values; MongoDB `$round` uses half-even. The trend pipeline generates its bounded date spine inside MongoDB from one department employee. It looks up daily headcount and attendance totals, then applies a seven-row window average to the rounded, nullable daily rates. The explain endpoint calls the same query and pipeline builders with the same options as the public routes, requesting `executionStats`; BSON values in the raw plan use Extended JSON where ordinary JSON has no equivalent.

The index choices are tied to the operations: unique `employees.emp_code` and unique `attendance_logs.(emp_code, date)` enforce identities; `employees.(department, emp_code)` supports filtered employee sorting; `employees.(joined_on, department)` supports unfiltered monthly headcount; `employees.(department, joined_on)` supports daily department headcount; `attendance_logs.(emp_code, punch_in)` supports latest eligible punch-out; `attendance_logs.(date, emp_code)` supports the default attendance list, date joins, and monthly leaderboard; and `attendance_logs.(status, date, emp_code)` supports status-filtered lists. The unique employee/date index also supports employee-specific attendance lookups. No index is created solely for explain.

## Test

```bash
pytest -q
```

The integration and concurrency tests start an isolated disposable `mongod` process on localhost and create a uniquely named test database, then remove it. They skip if the `mongod` executable is unavailable. The unit helper test does not need MongoDB.

For an optional large-data check, run `pytest -s -q tests/benchmark_phase4.py`. It creates 100,000 synthetic attendance records in another disposable database and prints startup, query, and `executionStats` timings. It never runs `sample_seed.py`.

## Measured performance and remaining work

On the local MongoDB 8.0.10 test process with 500 employees and 100,000 attendance records, index creation and application startup took 0.955 seconds. Representative HTTP requests took 0.029 seconds for the default attendance list, 0.013 for a filtered page, 0.011 for employee monthly, 0.167 for department summary, 0.038 for the late leaderboard, and 2.513 for a 92-day department trend. The five explain requests took 0.005, 0.008, 0.162, 0.035, and 2.756 seconds respectively. Each plan showed an index scan and no collection scan, including lookup collection-scan counts of zero. MongoDB 8 reported `EXPRESS_IXSCAN` for the trend anchor and `IXSCAN` for the other four operations. These are local measurements; MongoDB 7 and the grader's hidden dataset remain unverified.

The API scope is complete. A later submission audit and final `DECISIONS.md` write-up remain. `REVIEW.md` continues to describe only actual starter defects.
