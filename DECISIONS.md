# Decisions

1. **Indexes.** Unique employee code and attendance `(emp_code, date)` prevent duplicates. Employee `(department, emp_code)` supports filtered pages; `(joined_on, department)` supports monthly headcount; `(department, joined_on)` supports department headcount. Attendance `(emp_code, punch_in descending)` finds the latest eligible punch-in; `(date descending, emp_code)` supports ordered lists and date analytics; `(status, date descending, emp_code)` supports status-filtered lists. The unique attendance index also serves employee lookups. I rejected a standalone `status` index because the compound index serves filtered lists and another index adds write cost.

2. **Punch-in race.** Both requests may read the employee and build the same date. The unique `(emp_code, date)` index accepts one insert; the other gets `DuplicateKeyError`, returned as 409. The winner returns 201.

3. **Ties.** MongoDB competition rank gives equal late totals the same rank. Filtering by rank after grouping includes every employee tied at the cutoff, so the response can exceed `limit`.

4. **Headcount.** Department summary starts from employees, then looks up logs. Employees with no matching logs remain in the pipeline and count toward headcount with zero attendance totals.

5. **At 100 times the data.** I would precompute daily department and employee aggregates, update them when logs change, and measure reconciliation costs.
