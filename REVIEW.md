# Review of the starter code

| # | Where | Problem | How to spot it | Change made |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It used local, timezone-naive times, so results could change with the server's timezone. | Try a 09:40:01 IST punch on a UTC server. | Convert the shift boundary to UTC and truncate the punch time to seconds. |
| 2 | `compute_late_minutes` | Late minutes must be counted from shift start, but only after the full ten-minute grace period. | For a 09:30 shift, compare 09:40:00 with 09:40:01. | Apply the strict 600-second check, then floor elapsed minutes from 09:30. |
| 3 | `compute_work_hours` | Python's `round` uses ties-to-even, which does not match the required half-up rule. | Use a duration that lands exactly halfway between two hundredths. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | An overnight shift ends on the next day, not the punch-in date. | Check a 22:00–06:00 shift punched out at 06:40. | Move the shift-end boundary to the following day for overnight shifts. |
| 5 | `create_employee` | Checking for a duplicate before inserting does not protect against two requests arriving together. | Send two creates with the same employee code at once. | Add a unique index on `emp_code` and return 409 for a duplicate. |
| 6 | `create_employee` | The starter accepted malformed employee details and stored a naive creation time. | Try a bad email, shift time, date, or identical shift start and end. | Validate the fields and store `created_at` as a UTC datetime. |
| 7 | `list_employees` | The page offset was calculated as `page * page_size`, which skips the first page. | Request page 1 with a page size of 20. | Use `(page - 1) * page_size`. |
| 8 | `list_employees` | The total did not use the department filter. | Compare the total with the number of employees in one department. | Count using the same filter as the page query. |
| 9 | `list_employees` | Paging had no stable sort or limit on `page_size`. | Repeat a page request or ask for 10,000 rows. | Sort by employee code and limit page size to 100. |
| 10 | `punch_in` | An unknown employee could lead to an error while reading their shift. | Punch in with a code that is not in `employees`. | Check for the employee first and return 404. |
| 11 | `punch_in` | Timestamps were interpreted using the machine's timezone and were not strictly checked as milliseconds. | Try seconds, a float, or a punch with fractional seconds. | Validate integer epoch milliseconds and store the UTC time truncated to seconds. |
| 12 | `punch_in` | A check followed by an insert can create duplicates under concurrent requests. | Send two punch-ins for one employee and date at once. | Let the unique `(emp_code, date)` index decide which request succeeds; return 409 for the other. |
| 13 | `list_attendance` | It loaded matching rows before sorting and paging them in Python. | Try the endpoint with a large attendance collection. | Count, sort, skip, and limit in MongoDB. |
| 14 | `list_attendance` | Dates were not checked and the result order was not fully defined. | Try an invalid or reversed date range, or records with the same date. | Validate the dates and sort by date descending, then employee code. |
| 15 | Response handling | MongoDB datetime values and `_id` could leak into responses, despite the API requiring epoch milliseconds and no IDs. | Inspect employee, attendance, and correction responses. | Convert times at the API boundary and leave `_id` out of responses. |
| 16 | Missing routes | Punch-out, corrections, analytics, and explain were not implemented. | Call the routes listed in the assignment contract. | Add the routes, using atomic updates for writes and aggregation pipelines for analytics. |

## Parts that were already sound

The starter used `emp_code` as the employee identifier and kept the attendance date as a separate calendar value. That matches the data model. Its inclusive date range idea (`$gte` and `$lte`) was also right once date validation was added. I changed the health route because it needs to ping MongoDB before reporting success.
