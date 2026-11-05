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
