# SQL root-cause analysis

Eight queries (CTEs, window functions, gaps-and-islands, ROLLUP) that turn the failure ledger into a post-mortem: which errors
dominate, when they happen, how long incidents last, whether retrying helps, and whether the retry policy is followed.

**The history is simulated.** `uv run lab history` replays two weeks of operations (seed 7) through the same store the live
stack uses, so queueing, backoff, dead-lettering, audits and alert cool-downs are the real code. Only the world is scripted:
which request meets which fault, and when a person acts. The database holds 1,344 ingest runs, 85 retry runs and 227 ledger events.

The queries never read the timeline below. `tests/test_rca.py` checks that they find it blind.

**Scripted timeline (ground truth, UTC)**

| window | what happens | fault |
|---|---|---|
| every day 07:00 to 07:30 | the source is slow every morning from 07:00 to 07:30 UTC | `timeout` |
| Wed 14 Oct 15:20 to Wed 14 Oct 18:50 | the source returns 503 for three and a half hours | `http_5xx` |
| Sat 17 Oct 17:20 to Sun 18 Oct 11:00 | the source renamed the value field v to value | `schema_drift` |
| Tue 20 Oct 04:00 to Tue 20 Oct 09:00 | the source sends null for the CNY rate in the newest row | `null_value` |
| Thu 22 Oct 09:00 to Thu 22 Oct 09:20 | the source answers 429 with Retry-After 600 | `rate_limited` |
| Fri 23 Oct 14:15 to Fri 23 Oct 14:50 | the source restates yesterday's rates 8% higher, then reverts | `audit_jump` |

Reproduce this page with `uv run lab history && uv run lab rca --markdown docs/rca-report.md`. All times are UTC.

## 01. Failure rate by type

**Question.** Which error types cause the failures, and how much of the traffic does each one touch?

| error_type | kind | runs_hit | pct_of_runs | queue_items | rank | cumulative_pct | first_seen | last_seen |
|---|---|---|---|---|---|---|---|---|
| schema_drift | dataset | 68 | 5.06 | 1 | 1 | 49.6 | 2026-10-17 17:30 | 2026-10-18 10:45 |
| timeout | fetch | 26 | 1.93 | 14 | 2 | 68.6 | 2026-10-12 07:00 | 2026-10-25 07:15 |
| http_5xx | fetch | 20 | 1.49 | 7 | 3 | 83.2 | 2026-10-13 01:45 | 2026-10-25 21:00 |
| null_value | row | 18 | 1.34 | 1 | 4 | 96.4 | 2026-10-20 04:00 | 2026-10-20 08:45 |
| audit_jump | audit | 3 | 0.22 | 1 | 5 | 98.5 | 2026-10-23 14:15 | 2026-10-23 14:45 |
| rate_limited | fetch | 2 | 0.15 | 1 | 6 | 100 | 2026-10-22 09:00 | 2026-10-22 09:15 |

**How to read it.** runs_hit is the number of ingest runs that saw the type at least once. The cumulative column is the usual Pareto cut: fix the first few rows and most of the pain goes away.

**Technique.** a CTE for the denominator, count DISTINCT per type, RANK() and a running share (window over an aggregate).

<details><summary>The SQL (01_failure_rate_by_type.sql)</summary>

```sql
-- question:  Which error types cause the failures, and how much of the traffic does each one touch?
-- technique: a CTE for the denominator, count DISTINCT per type, RANK() and a running share (window over an aggregate).
-- reading:   runs_hit is the number of ingest runs that saw the type at least once. The cumulative column is the
--            usual Pareto cut: fix the first few rows and most of the pain goes away.
WITH traffic AS (
    SELECT count(*) AS runs FROM runs WHERE workflow = 'ingest'
),
by_type AS (
    SELECT e.error_type,
           e.kind,
           count(DISTINCT e.run_id) AS runs_hit,
           count(DISTINCT e.failure_id) AS queue_items,
           min(e.ts) AS first_seen,
           max(e.ts) AS last_seen
    FROM failure_events e
    JOIN runs r ON r.run_id = e.run_id AND r.workflow = 'ingest'
    WHERE e.event = 'detected'
    GROUP BY e.error_type, e.kind
)
SELECT b.error_type,
       b.kind,
       b.runs_hit,
       round(100.0 * b.runs_hit / t.runs, 2) AS pct_of_runs,
       b.queue_items,
       rank() OVER (ORDER BY b.runs_hit DESC) AS rank,
       round(100.0 * sum(b.runs_hit) OVER (ORDER BY b.runs_hit DESC, b.error_type) / sum(b.runs_hit) OVER (), 1) AS cumulative_pct,
       b.first_seen,
       b.last_seen
FROM by_type b
CROSS JOIN traffic t
ORDER BY b.runs_hit DESC, b.error_type;
```

</details>

## 02. Failure rate by hour

**Question.** At what time of day do runs fail, and is it a pattern or a one-off?

| hour_utc | runs | bad_runs | bad_pct | overall_pct | days_with_failures | flag | main_error |
|---|---|---|---|---|---|---|---|
| 0 | 56 | 4 | 7.1 | 10.2 | 1 of 14 |  |  |
| 1 | 56 | 5 | 8.9 | 10.2 | 2 of 14 |  |  |
| 2 | 56 | 5 | 8.9 | 10.2 | 2 of 14 |  |  |
| 3 | 56 | 4 | 7.1 | 10.2 | 1 of 14 |  |  |
| 4 | 56 | 8 | 14.3 | 10.2 | 2 of 14 |  |  |
| 5 | 56 | 8 | 14.3 | 10.2 | 2 of 14 |  |  |
| 6 | 56 | 8 | 14.3 | 10.2 | 2 of 14 |  |  |
| 7 | 56 | 30 | 53.6 | 10.2 | 14 of 14 | RECURRING | timeout |
| 8 | 56 | 8 | 14.3 | 10.2 | 2 of 14 |  |  |
| 9 | 56 | 6 | 10.7 | 10.2 | 2 of 14 |  |  |
| 10 | 56 | 4 | 7.1 | 10.2 | 1 of 14 |  |  |
| 11 | 56 | 0 | 0 | 10.2 | 0 of 14 |  |  |
| 12 | 56 | 0 | 0 | 10.2 | 0 of 14 |  |  |
| 13 | 56 | 0 | 0 | 10.2 | 0 of 14 |  |  |
| 14 | 56 | 3 | 5.4 | 10.2 | 1 of 14 |  |  |
| 15 | 56 | 2 | 3.6 | 10.2 | 1 of 14 |  |  |
| 16 | 56 | 5 | 8.9 | 10.2 | 2 of 14 |  |  |
| 17 | 56 | 6 | 10.7 | 10.2 | 2 of 14 |  |  |
| 18 | 56 | 8 | 14.3 | 10.2 | 2 of 14 |  |  |
| 19 | 56 | 5 | 8.9 | 10.2 | 2 of 14 |  |  |
| 20 | 56 | 4 | 7.1 | 10.2 | 1 of 14 |  |  |
| 21 | 56 | 5 | 8.9 | 10.2 | 2 of 14 |  |  |
| 22 | 56 | 4 | 7.1 | 10.2 | 1 of 14 |  |  |
| 23 | 56 | 5 | 8.9 | 10.2 | 2 of 14 |  |  |

**How to read it.** RECURRING = the hour fails more than twice as often as average AND on at least half of the days: a schedule on the other side (maintenance window, batch job), not an incident. spike = a hot hour that did not recur.

**Technique.** generate_series as a time spine (quiet hours show 0 instead of vanishing), FILTER aggregates, a window average as the baseline, arg_max for the error seen on the most distinct days.

<details><summary>The SQL (02_failure_rate_by_hour.sql)</summary>

```sql
-- question:  At what time of day do runs fail, and is it a pattern or a one-off?
-- technique: generate_series as a time spine (quiet hours show 0 instead of vanishing), FILTER aggregates, a window
--            average as the baseline, arg_max for the error seen on the most distinct days.
-- reading:   RECURRING = the hour fails more than twice as often as average AND on at least half of the days: a
--            schedule on the other side (maintenance window, batch job), not an incident. spike = a hot hour that
--            did not recur.
WITH spine AS (
    SELECT h FROM generate_series(0, 23) AS t(h)
),
by_hour AS (
    SELECT date_part('hour', started_at)::INT AS h,
           count(*) AS runs,
           count(*) FILTER (WHERE bad = 1) AS bad_runs,
           count(DISTINCT started_at::DATE) FILTER (WHERE bad = 1) AS days_hit,
           count(DISTINCT started_at::DATE) AS days_total
    FROM v_ingest_runs
    GROUP BY 1
),
cause AS (
    SELECT h, arg_max(error_type, days) AS main_error
    FROM (
        SELECT date_part('hour', e.ts)::INT AS h, e.error_type, count(DISTINCT e.ts::DATE) AS days
        FROM failure_events e
        JOIN runs r ON r.run_id = e.run_id AND r.workflow = 'ingest'
        WHERE e.event = 'detected'
        GROUP BY 1, 2
    )
    GROUP BY h
),
rates AS (
    SELECT s.h,
           coalesce(b.runs, 0) AS runs,
           coalesce(b.bad_runs, 0) AS bad_runs,
           coalesce(b.days_hit, 0) AS days_hit,
           b.days_total,
           100.0 * coalesce(b.bad_runs, 0) / nullif(b.runs, 0) AS bad_pct,
           100.0 * sum(coalesce(b.bad_runs, 0)) OVER () / nullif(sum(coalesce(b.runs, 0)) OVER (), 0) AS overall_pct
    FROM spine s
    LEFT JOIN by_hour b USING (h)
)
SELECT r.h AS hour_utc,
       r.runs,
       r.bad_runs,
       round(r.bad_pct, 1) AS bad_pct,
       round(r.overall_pct, 1) AS overall_pct,
       r.days_hit || ' of ' || r.days_total AS days_with_failures,
       CASE WHEN r.bad_pct > 2 * r.overall_pct AND r.days_hit >= 0.5 * r.days_total THEN 'RECURRING'
            WHEN r.bad_pct > 2 * r.overall_pct THEN 'spike'
            ELSE '' END AS flag,
       CASE WHEN r.bad_pct > 2 * r.overall_pct THEN c.main_error END AS main_error
FROM rates r
LEFT JOIN cause c USING (h)
ORDER BY r.h;
```

</details>

## 03. Daily trend

**Question.** Is the failure rate trending, and which days stand out?

| day | weekday | runs | bad_runs | bad_pct | worst_1h_pct | worst_1h_at | avg_7d_pct | flag |
|---|---|---|---|---|---|---|---|---|
| 2026-10-12 | Monday | 96 | 2 | 2.1 | 40 | 2026-10-12 07:15 | 2.1 |  |
| 2026-10-13 | Tuesday | 96 | 3 | 3.1 | 40 | 2026-10-13 07:15 | 2.6 |  |
| 2026-10-14 | Wednesday | 96 | 16 | 16.7 | 100 | 2026-10-14 16:30 | 7.3 | SPIKE |
| 2026-10-15 | Thursday | 96 | 3 | 3.1 | 40 | 2026-10-15 07:15 | 6.2 |  |
| 2026-10-16 | Friday | 96 | 2 | 2.1 | 40 | 2026-10-16 07:15 | 5.4 |  |
| 2026-10-17 | Saturday | 96 | 27 | 28.1 | 100 | 2026-10-17 18:30 | 9.2 | SPIKE |
| 2026-10-18 | Sunday | 96 | 44 | 45.8 | 100 | 2026-10-18 00:00 | 14.4 | SPIKE |
| 2026-10-19 | Monday | 96 | 3 | 3.1 | 40 | 2026-10-19 07:15 | 14.6 |  |
| 2026-10-20 | Tuesday | 96 | 20 | 20.8 | 100 | 2026-10-20 05:00 | 17.1 | SPIKE |
| 2026-10-21 | Wednesday | 96 | 2 | 2.1 | 40 | 2026-10-21 07:15 | 15 |  |
| 2026-10-22 | Thursday | 96 | 3 | 3.1 | 40 | 2026-10-22 09:15 | 15 |  |
| 2026-10-23 | Friday | 96 | 5 | 5.2 | 60 | 2026-10-23 14:45 | 15.5 |  |
| 2026-10-24 | Saturday | 96 | 2 | 2.1 | 40 | 2026-10-24 07:15 | 11.8 |  |
| 2026-10-25 | Sunday | 96 | 5 | 5.2 | 40 | 2026-10-25 07:15 | 6 |  |

**How to read it.** SPIKE = the day failed more than twice as often as the MEDIAN of the six days before it. The median is the baseline because one bad week would otherwise poison an average and hide the next incident.

**Technique.** named WINDOW clauses with time-based RANGE frames (the worst rolling hour of each day), ROW_NUMBER to pick that hour deterministically, a 7-day moving average over the daily rates, and a spike test against the previous week.

<details><summary>The SQL (03_daily_trend.sql)</summary>

```sql
-- question:  Is the failure rate trending, and which days stand out?
-- technique: named WINDOW clauses with time-based RANGE frames (the worst rolling hour of each day), ROW_NUMBER to pick
--            that hour deterministically, a 7-day moving average over the daily rates, and a spike test against the
--            previous week.
-- reading:   SPIKE = the day failed more than twice as often as the MEDIAN of the six days before it. The median is the
--            baseline because one bad week would otherwise poison an average and hide the next incident.
WITH rolling AS (
    SELECT started_at,
           bad,
           avg(bad) OVER hour AS rate_1h
    FROM v_ingest_runs
    WINDOW hour AS (ORDER BY started_at RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND CURRENT ROW)
),
worst AS (
    SELECT started_at::DATE AS day, started_at AS worst_1h_at, rate_1h AS worst_1h
    FROM rolling
    QUALIFY row_number() OVER (PARTITION BY started_at::DATE ORDER BY rate_1h DESC, started_at) = 1
),
daily AS (
    SELECT started_at::DATE AS day, count(*) AS runs, sum(bad) AS bad_runs, 100.0 * avg(bad) AS bad_pct
    FROM rolling
    GROUP BY 1
)
SELECT d.day,
       dayname(d.day) AS weekday,
       d.runs,
       d.bad_runs,
       round(d.bad_pct, 1) AS bad_pct,
       round(100 * w.worst_1h) AS worst_1h_pct,
       w.worst_1h_at,
       round(avg(d.bad_pct) OVER (ORDER BY d.day ROWS BETWEEN 6 PRECEDING AND CURRENT ROW), 1) AS avg_7d_pct,
       CASE WHEN d.bad_pct > 2 * quantile_cont(d.bad_pct, 0.5) OVER (ORDER BY d.day ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING) THEN 'SPIKE' ELSE '' END AS flag
FROM daily d
JOIN worst w USING (day)
ORDER BY d.day;
```

</details>

## 04. Incidents

**Question.** What were the incidents, how long did each last, and how quickly did anyone hear about it?

| incident | started_at | recovered_at | minutes_to_recover | bad_runs | main_error | first_alert_at | minutes_to_alert |
|---|---|---|---|---|---|---|---|
| 1 | 2026-10-12 07:00 | 2026-10-12 07:30 | 30 | 2 | timeout |  |  |
| 3 | 2026-10-13 07:00 | 2026-10-13 07:30 | 30 | 2 | timeout |  |  |
| 4 | 2026-10-14 07:00 | 2026-10-14 07:30 | 30 | 2 | timeout |  |  |
| 5 | 2026-10-14 15:30 | 2026-10-14 19:00 | 210 | 14 | http_5xx | 2026-10-14 16:35 | 65 |
| 6 | 2026-10-15 07:00 | 2026-10-15 07:30 | 30 | 2 | timeout |  |  |
| 8 | 2026-10-16 07:00 | 2026-10-16 07:30 | 30 | 2 | timeout |  |  |
| 10 | 2026-10-17 17:30 | 2026-10-18 11:00 | 1050 | 70 | schema_drift | 2026-10-17 17:30 | 0 |
| 12 | 2026-10-19 07:00 | 2026-10-19 07:30 | 30 | 2 | timeout |  |  |
| 13 | 2026-10-20 04:00 | 2026-10-20 09:00 | 300 | 20 | null_value | 2026-10-20 05:10 | 70 |
| 14 | 2026-10-21 07:00 | 2026-10-21 07:30 | 30 | 2 | timeout |  |  |
| 16 | 2026-10-22 09:00 | 2026-10-22 09:30 | 30 | 2 | rate_limited |  |  |
| 17 | 2026-10-23 07:00 | 2026-10-23 07:30 | 30 | 2 | timeout |  |  |
| 18 | 2026-10-23 14:15 | 2026-10-23 15:00 | 45 | 3 | audit_jump | 2026-10-23 14:15 | 0 |
| 19 | 2026-10-24 07:00 | 2026-10-24 07:30 | 30 | 2 | timeout |  |  |
| 20 | 2026-10-25 07:00 | 2026-10-25 07:30 | 30 | 2 | timeout |  |  |

**How to read it.** an incident is a streak of at least two consecutive ingest runs that did not fully succeed. Single bad runs are blips and are counted in the footer query of the runbook, not listed here.

**Technique.** gaps and islands (the v_incidents view), mode() for the dominant error, a range join to the alert ledger.

<details><summary>The SQL (04_incidents.sql)</summary>

```sql
-- question:  What were the incidents, how long did each last, and how quickly did anyone hear about it?
-- technique: gaps and islands (the v_incidents view), mode() for the dominant error, a range join to the alert ledger.
-- reading:   an incident is a streak of at least two consecutive ingest runs that did not fully succeed. Single bad
--            runs are blips and are counted in the footer query of the runbook, not listed here.
WITH causes AS (
    SELECT r.streak AS incident, mode(e.error_type) AS main_error
    FROM v_ingest_runs r
    JOIN failure_events e ON e.run_id = r.run_id AND e.event = 'detected'
    WHERE r.bad = 1
    GROUP BY 1
),
first_alert AS (
    SELECT i.incident, min(a.ts) AS first_alert_at
    FROM v_incidents i
    JOIN alerts a ON a.sent AND a.ts BETWEEN i.started_at AND coalesce(i.recovered_at, i.last_bad_run_at + INTERVAL 1 HOUR)
    GROUP BY 1
)
SELECT i.incident,
       i.started_at,
       i.recovered_at,
       round(epoch(i.recovered_at - i.started_at) / 60) AS minutes_to_recover,
       i.bad_runs,
       c.main_error,
       a.first_alert_at,
       round(epoch(a.first_alert_at - i.started_at) / 60) AS minutes_to_alert
FROM v_incidents i
LEFT JOIN causes c USING (incident)
LEFT JOIN first_alert a USING (incident)
WHERE i.bad_runs >= 2
ORDER BY i.started_at;
```

</details>

## 05. Error patterns

**Question.** For each error type: when did it first appear, how long did it last, and is it chronic or episodic?

| error_type | kind | first_day | last_day | active_days | days_total | pattern | runs_hit | worst_day | worst_day_runs | runbook |
|---|---|---|---|---|---|---|---|---|---|---|
| timeout | fetch | 2026-10-12 | 2026-10-25 | 14 | 14 | chronic | 26 | 2026-10-24 | 2 | timeout |
| http_5xx | fetch | 2026-10-13 | 2026-10-25 | 5 | 14 | episodic | 20 | 2026-10-14 | 14 | http-5xx |
| schema_drift | dataset | 2026-10-17 | 2026-10-18 | 2 | 14 | episodic | 68 | 2026-10-18 | 42 | schema-drift |
| null_value | row | 2026-10-20 | 2026-10-20 | 1 | 14 | one-off | 18 | 2026-10-20 | 18 | null-value |
| rate_limited | fetch | 2026-10-22 | 2026-10-22 | 1 | 14 | one-off | 2 | 2026-10-22 | 2 | rate-limited |
| audit_jump | audit | 2026-10-23 | 2026-10-23 | 1 | 14 | one-off | 3 | 2026-10-23 | 3 | audit-jump |

**How to read it.** chronic = present on most days (a standing condition: a schedule, a limit, a bad default). episodic = a few bursts (an incident). one-off = a single day. first_day is the change point: look at what deployed or changed upstream just before it.

**Technique.** a daily CTE, min/max/count per type, a window LAG for the day-over-day step, arg_max for the worst day.

<details><summary>The SQL (05_error_patterns.sql)</summary>

```sql
-- question:  For each error type: when did it first appear, how long did it last, and is it chronic or episodic?
-- technique: a daily CTE, min/max/count per type, a window LAG for the day-over-day step, arg_max for the worst day.
-- reading:   chronic = present on most days (a standing condition: a schedule, a limit, a bad default). episodic =
--            a few bursts (an incident). one-off = a single day. first_day is the change point: look at what
--            deployed or changed upstream just before it.
WITH daily AS (
    SELECT e.error_type, e.ts::DATE AS day, count(DISTINCT e.run_id) AS runs_hit
    FROM failure_events e
    JOIN runs r ON r.run_id = e.run_id AND r.workflow = 'ingest'
    WHERE e.event = 'detected'
    GROUP BY 1, 2
),
stepped AS (
    SELECT *, runs_hit - coalesce(lag(runs_hit) OVER (PARTITION BY error_type ORDER BY day), 0) AS step_vs_prev_active_day
    FROM daily
),
span AS (
    SELECT count(DISTINCT started_at::DATE) AS days_total FROM runs WHERE workflow = 'ingest'
)
SELECT s.error_type,
       t.kind,
       min(s.day) AS first_day,
       max(s.day) AS last_day,
       count(*) AS active_days,
       p.days_total,
       CASE WHEN count(*) >= 0.8 * p.days_total THEN 'chronic'
            WHEN count(*) = 1 THEN 'one-off'
            ELSE 'episodic' END AS pattern,
       sum(s.runs_hit) AS runs_hit,
       arg_max(s.day, s.runs_hit) AS worst_day,
       max(s.runs_hit) AS worst_day_runs,
       t.runbook
FROM stepped s
JOIN error_types t USING (error_type)
CROSS JOIN span p
GROUP BY s.error_type, t.kind, p.days_total, t.runbook
ORDER BY first_day, s.error_type;
```

</details>

## 06. Retry effectiveness

**Question.** Does retrying work? How many failures heal by themselves, after how many attempts, and how long does it take?

| error_type | retry_budget | failures | healed_on_first_retry | healed_later | healed_after_a_person | dead_now | still_open | self_healed_pct | median_minutes | p90_minutes | times_seen |
|---|---|---|---|---|---|---|---|---|---|---|---|
| audit_jump | 3 | 1 | 0 | 1 | 0 | 0 | 0 | 100 | 75 | 75 | 3 |
| http_5xx | 6 | 7 | 6 | 0 | 1 | 0 | 0 | 86 | 5 | 89 | 20 |
| null_value | 4 | 1 | 0 | 0 | 0 | 1 | 0 | 0 | 70 | 70 | 18 |
| rate_limited | 8 | 1 | 0 | 1 | 0 | 0 | 0 | 100 | 20 | 20 | 2 |
| schema_drift | 1 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 1060 | 1060 | 68 |
| timeout | 6 | 14 | 0 | 14 | 0 | 0 | 0 | 100 | 35 | 40 | 26 |
| ALL | 6 | 25 | 6 | 16 | 2 | 1 | 0 | 88 | 35 | 73 | 137 |

**How to read it.** a type with a high dead share and few early resolutions is burning retries for nothing: either its budget is shorter than the usual outage, or retrying cannot help and it should be non-retryable. The time to close for requeued failures includes the human response time.

**Technique.** FILTER aggregates over the queue, a semi-join to the ledger for failures a person had to requeue, quantile_cont for the median and p90 time-to-close, a join to the taxonomy for the retry budget, ROLLUP for the total row.

<details><summary>The SQL (06_retry_effectiveness.sql)</summary>

```sql
-- question:  Does retrying work? How many failures heal by themselves, after how many attempts, and how long does it take?
-- technique: FILTER aggregates over the queue, a semi-join to the ledger for failures a person had to requeue,
--            quantile_cont for the median and p90 time-to-close, a join to the taxonomy for the retry budget, ROLLUP for
--            the total row.
-- reading:   a type with a high dead share and few early resolutions is burning retries for nothing: either its budget
--            is shorter than the usual outage, or retrying cannot help and it should be non-retryable. The time to
--            close for requeued failures includes the human response time.
WITH closed AS (
    SELECT q.failure_id,
           q.error_type,
           q.status,
           q.attempts,
           q.occurrences,
           q.failure_id IN (SELECT failure_id FROM failure_events WHERE event = 'requeued') AS was_requeued,
           epoch(coalesce(q.resolved_at, q.dead_at) - q.first_seen_at) / 60.0 AS minutes_to_close
    FROM failure_queue q
)
SELECT coalesce(c.error_type, 'ALL') AS error_type,
       any_value(t.max_attempts) AS retry_budget,
       count(*) AS failures,
       count(*) FILTER (WHERE c.status = 'resolved' AND NOT c.was_requeued AND c.attempts <= 1) AS healed_on_first_retry,
       count(*) FILTER (WHERE c.status = 'resolved' AND NOT c.was_requeued AND c.attempts >= 2) AS healed_later,
       count(*) FILTER (WHERE c.status = 'resolved' AND c.was_requeued) AS healed_after_a_person,
       count(*) FILTER (WHERE c.status = 'dead') AS dead_now,
       count(*) FILTER (WHERE c.status IN ('pending', 'in_progress')) AS still_open,
       round(100.0 * count(*) FILTER (WHERE c.status = 'resolved' AND NOT c.was_requeued) / count(*)) AS self_healed_pct,
       round(quantile_cont(c.minutes_to_close, 0.5)) AS median_minutes,
       round(quantile_cont(c.minutes_to_close, 0.9)) AS p90_minutes,
       sum(c.occurrences) AS times_seen
FROM closed c
JOIN error_types t USING (error_type)
GROUP BY ROLLUP (c.error_type)
ORDER BY c.error_type NULLS LAST;
```

</details>

## 07. Backoff audit

**Question.** Is the retry policy actually followed? Did any retry fire early, are the planned waits inside the policy's envelope, and how late does the worker run?

| error_type | retries | fired_early | late_median_s | late_p95_s | outside_envelope | verdict |
|---|---|---|---|---|---|---|
| ALL | 62 | 0 | 238 | 269 | 0 | PASS |
| http_5xx | 9 | 0 | 266 | 268 | 0 | PASS |
| null_value | 1 | 0 | 64 | 64 | 0 | PASS |
| rate_limited | 1 | 0 | 0 | 0 | 0 | PASS |
| timeout | 51 | 0 | 236 | 277 | 0 | PASS |

**How to read it.** fired_early must be 0. late_p95 shows how far behind schedule the worker runs: the worker polls every five minutes, so up to 300 s is normal and more means it is falling behind. rate_limited is left out of the envelope check because Retry-After may legitimately push a wait past the ceiling.

**Technique.** LAG over each failure's ledger to pair every attempt with the due time and wait planned before it, then quantile_cont and FILTER counts. The expected envelope comes from the taxonomy: wait n lies between half of min(cap, base * 2^(n-1)) and that value (equal jitter).

<details><summary>The SQL (07_backoff_audit.sql)</summary>

```sql
-- question:  Is the retry policy actually followed? Did any retry fire early, are the planned waits inside the policy's
--            envelope, and how late does the worker run?
-- technique: LAG over each failure's ledger to pair every attempt with the due time and wait planned before it, then
--            quantile_cont and FILTER counts. The expected envelope comes from the taxonomy: wait n lies between
--            half of min(cap, base * 2^(n-1)) and that value (equal jitter).
-- reading:   fired_early must be 0. late_p95 shows how far behind schedule the worker runs: the worker polls every five
--            minutes, so up to 300 s is normal and more means it is falling behind. rate_limited is left out of the
--            envelope check because Retry-After may legitimately push a wait past the ceiling.
WITH steps AS (
    SELECT e.failure_id, e.event, e.attempt, e.ts,
           lag(e.next_retry_at) OVER ledger AS due_at,
           lag(e.retry_in_s) OVER ledger AS planned_wait_s
    FROM failure_events e
    WHERE e.event IN ('detected', 'retry_failed', 'retry_ok')
    WINDOW ledger AS (PARTITION BY e.failure_id ORDER BY e.event_id)
),
attempts AS (
    SELECT q.error_type,
           epoch(s.ts - s.due_at) AS late_s,
           s.planned_wait_s,
           least(t.cap_s, t.base_s * power(2, s.attempt - 1)) AS ceiling_s
    FROM steps s
    JOIN failure_queue q USING (failure_id)
    JOIN error_types t ON t.error_type = q.error_type
    WHERE s.event IN ('retry_failed', 'retry_ok') AND s.due_at IS NOT NULL
)
SELECT coalesce(error_type, 'ALL') AS error_type,
       count(*) AS retries,
       count(*) FILTER (WHERE late_s < -1) AS fired_early,
       round(quantile_cont(late_s, 0.5)) AS late_median_s,
       round(quantile_cont(late_s, 0.95)) AS late_p95_s,
       count(*) FILTER (WHERE error_type <> 'rate_limited' AND (planned_wait_s < 0.5 * ceiling_s - 1 OR planned_wait_s > ceiling_s + 1)) AS outside_envelope,
       CASE WHEN count(*) FILTER (WHERE late_s < -1) = 0
             AND count(*) FILTER (WHERE error_type <> 'rate_limited' AND (planned_wait_s < 0.5 * ceiling_s - 1 OR planned_wait_s > ceiling_s + 1)) = 0
             AND quantile_cont(late_s, 0.95) <= 330 THEN 'PASS' ELSE 'CHECK' END AS verdict
FROM attempts
GROUP BY ROLLUP (error_type)
ORDER BY error_type NULLS LAST;
```

</details>

## 08. Post mortem

**Question.** The post-mortem table: every incident with its likely cause, whether anyone was told, and who fixed it.

| what | first_started_at | typical_minutes_down | bad_runs | main_error | severity | what_it_means | minutes_to_alert | outcome | runbook |
|---|---|---|---|---|---|---|---|---|---|
| 10 incidents (a daily pattern) | 2026-10-12 07:00 | 30 | 20 | timeout | warning | The source did not answer within the request timeout |  | self-healed by retries | timeout |
| incident 5 | 2026-10-14 15:30 | 210 | 14 | http_5xx | warning | The source answered with a 5xx status | 65 | fixed by a person (requeued) | http-5xx |
| incident 10 | 2026-10-17 17:30 | 1050 | 70 | schema_drift | critical | The payload shape changed (field renamed or moved) | 0 | fixed by a person (requeued) | schema-drift |
| incident 13 | 2026-10-20 04:00 | 300 | 20 | null_value | warning | A value is null, empty or absent in one row | 70 | dead-lettered, needs a person | null-value |
| incident 16 | 2026-10-22 09:00 | 30 | 2 | rate_limited | warning | The source answered 429 Too Many Requests |  | self-healed by retries | rate-limited |
| incident 18 | 2026-10-23 14:15 | 45 | 3 | audit_jump | critical | Too many series moved too far in one day, so the batch was held before publishing | 0 | self-healed by retries | audit-jump |

**How to read it.** minutes_to_alert is how long the first alert took; null means nobody was paged because retries fixed it. "needs a person" rows are the ones to put on the agenda.

**Technique.** CTEs over the incident view, range joins to the alert ledger and to dead-letter and requeue events, the taxonomy for the cause text and the runbook anchor, and one grouping key that folds a chronic cause (it happens every day) into a single row instead of listing it fourteen times.

<details><summary>The SQL (08_post_mortem.sql)</summary>

```sql
-- question:  The post-mortem table: every incident with its likely cause, whether anyone was told, and who fixed it.
-- technique: CTEs over the incident view, range joins to the alert ledger and to dead-letter and requeue events, the
--            taxonomy for the cause text and the runbook anchor, and one grouping key that folds a chronic cause (it
--            happens every day) into a single row instead of listing it fourteen times.
-- reading:   minutes_to_alert is how long the first alert took; null means nobody was paged because retries fixed it.
--            "needs a person" rows are the ones to put on the agenda.
WITH causes AS (
    SELECT r.streak AS incident, mode(e.error_type) AS main_error
    FROM v_ingest_runs r
    JOIN failure_events e ON e.run_id = r.run_id AND e.event = 'detected'
    WHERE r.bad = 1
    GROUP BY 1
),
chronic AS (
    SELECT e.error_type
    FROM failure_events e
    JOIN runs r ON r.run_id = e.run_id AND r.workflow = 'ingest'
    WHERE e.event = 'detected'
    GROUP BY 1
    HAVING count(DISTINCT e.ts::DATE) >= 0.8 * (SELECT count(DISTINCT started_at::DATE) FROM runs WHERE workflow = 'ingest')
),
window_of AS (
    SELECT incident, started_at, coalesce(recovered_at, last_bad_run_at) AS ended_at, recovered_at, bad_runs
    FROM v_incidents
    WHERE bad_runs >= 2
),
per_incident AS (
    SELECT w.incident, w.started_at, w.recovered_at, w.bad_runs, c.main_error,
           epoch(w.recovered_at - w.started_at) / 60 AS minutes_down,
           (SELECT min(a.ts) FROM alerts a WHERE a.sent AND a.ts BETWEEN w.started_at AND w.ended_at) AS first_alert_at,
           (SELECT count(*) FROM failure_events e WHERE e.event = 'dead_lettered' AND e.ts BETWEEN w.started_at AND w.ended_at + INTERVAL 2 HOUR) AS dead_letters,
           (SELECT count(*) FROM failure_events e WHERE e.event = 'requeued' AND e.ts BETWEEN w.started_at AND w.ended_at + INTERVAL 2 HOUR) AS requeues,
           c.main_error IN (SELECT error_type FROM chronic) AS is_chronic
    FROM window_of w
    LEFT JOIN causes c USING (incident)
)
SELECT CASE WHEN bool_or(p.is_chronic) THEN count(*) || ' incidents (a daily pattern)' ELSE 'incident ' || min(p.incident) END AS what,
       min(p.started_at) AS first_started_at,
       round(quantile_cont(p.minutes_down, 0.5)) AS typical_minutes_down,
       sum(p.bad_runs) AS bad_runs,
       p.main_error,
       t.severity,
       t.summary AS what_it_means,
       round(epoch(min(p.first_alert_at) - min(p.started_at)) / 60) AS minutes_to_alert,
       CASE WHEN sum(p.requeues) > 0 THEN 'fixed by a person (requeued)'
            WHEN sum(p.dead_letters) > 0 THEN 'dead-lettered, needs a person'
            ELSE 'self-healed by retries' END AS outcome,
       t.runbook
FROM per_incident p
JOIN error_types t ON t.error_type = p.main_error
GROUP BY CASE WHEN p.is_chronic THEN 'chronic ' || p.main_error ELSE 'incident ' || p.incident::VARCHAR END, p.main_error, t.severity, t.summary, t.runbook
ORDER BY first_started_at;
```

</details>
