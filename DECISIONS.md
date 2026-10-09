# Design decisions

1. **Indexes.** I added a unique index to employee code and a unique `(emp_code, date)` index to attendance. They prevent duplicate employees and daily records. The other indexes help with date searches, reports, punch-out lookups, and department filters. A separate status index would add little because there are only five status values.

2. **Two punch-ins at once.** MongoDB's unique index lets one request create the daily record and rejects the other. The API returns 409 for the rejected request.

3. **Leaderboard ties.** MongoDB's `$rank` gives employees with the same late total the same rank and skips the next rank. I apply the limit after ranking so ties at the cutoff remain in the results, then sort them by employee code.

4. **Employees without logs.** The summary starts with employees who joined by month-end and looks up their attendance. Employees with no logs still count in headcount; future employees do not.

5. **If data grew 100 times.** I would measure the queries on a larger dataset, use cursor paging for long lists, and consider saving monthly summaries if live aggregation became too slow.
