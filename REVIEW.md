# Phase 1 starter review

The locations below refer to the supplied starter `app/main.py`. This review covers the helpers and five Phase 1 routes; Phase 2 routes have not been implemented or reviewed as completed work.

| # | Starter location | Defect and reproducing example | Fix |
|---|---|---|---|
| 1 | `compute_late_minutes` | At 09:40:01 for a 09:30 shift, flooring before the grace check returns 0; R2 requires 10. | Compare elapsed seconds to 600 first, then floor minutes from shift start. |
| 2 | `compute_late_minutes` | Replacing the punch-in's local hour with the shift hour uses the wrong date/time zone and cannot handle an overnight attendance day. | Build an IST shift-start instant from the attendance date and compare UTC-aware instants. |
| 3 | `compute_work_hours` | Python `round()` uses half-even rounding and floats; a 1 hour 18 second shift must be 1.01. | Calculate with `Decimal` and `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Five minutes after shift end returns 5, but R3 requires 0 below 30. | Floor elapsed minutes and apply the 30-minute threshold. |
| 5 | `compute_overtime` | A 22:00–06:00 shift ending at 06:40 the next day is compared with 06:00 on the attendance date. | Move the shift-end instant to the next IST day for overnight shifts. |
| 6 | `EmployeeIn` | Invalid codes, email, empty names, impossible joined dates, or equal shift times pass validation. | Apply the exact schema constraints and validate the calendar date and distinct shift times. |
| 7 | `PunchInIn` | `status="ABSENT"`, seconds-looking timestamps, strings, floats and explicit null pass; `punched_at=0` silently uses now. | Use strict bounded integer milliseconds, a presence-status literal, and default only an omitted timestamp. |
| 8 | `/health` | It returns 200 with MongoDB stopped. | Ping MongoDB and return 503 on failure. |
| 9 | `/employees` POST | `find_one` followed by insert races; two concurrent requests can both pass the check. | Create a unique `emp_code` index at startup and map `DuplicateKeyError` to 409. |
| 10 | `/employees` POST | `datetime.now()` is naive and `created_at` is returned as an ISO date. | Store a UTC-aware server instant and serialize it as epoch milliseconds. |
| 11 | `/employees` GET | `skip=page*page_size` skips the first page; `count_documents({})` ignores department; no sort makes pages unstable. | Use `(page-1)*page_size`, count the filtered query, and sort by `emp_code`. |
| 12 | `/employees` GET | Invalid page sizes and page 0 are accepted; an empty department filter is silently ignored. | Enforce R10 limits and apply every supplied department value as an exact filter. |
| 13 | `/attendance/punch-in` | Unknown employees cause a 500 when `emp["shift_start"]` is accessed. | Return 404. |
| 14 | `/attendance/punch-in` | `datetime.fromtimestamp()` and `datetime.now()` are local naive times; a UTC timestamp near IST midnight lands on the wrong attendance date. | Convert milliseconds to UTC-aware time, truncate to seconds, then determine the IST attendance date. |
| 15 | `/attendance/punch-in` | A check-then-insert allows duplicate records during simultaneous requests. | Use a unique `(emp_code, date)` index and map insert conflicts to 409. |
| 16 | `/attendance/punch-in` | The response exposes an `id` and BSON datetime, violating the record shape. | Explicitly serialize only documented fields and epoch milliseconds. |
| 17 | `/attendance` GET | Fetches all matching documents into Python, then sorts and slices; this grows with the collection. | Filter, sort, count, skip, and limit in MongoDB. |
| 18 | `/attendance` GET | Sort only by date leaves same-day employee order unstable; dates and status are not validated. | Sort by date descending then code ascending; validate date, status, range, and pagination. |
| 19 | `/attendance` GET | Returns `id` and raw BSON datetimes and omits documented defaults from legacy records. | Explicit serialization converts nested history times, removes IDs, and supplies missing defaults. |

## Examined choices that are correct

- `date` and `joined_on` are strings in MongoDB. They represent IST calendar days, and ISO ordering supports date-range filters.
- Calling `load_dotenv()` without `override=True` preserves injected environment variables over a local `.env` file.
- Keeping absent and leave records out of punch-in is consistent with the status table. The API may still read such seeded records.
