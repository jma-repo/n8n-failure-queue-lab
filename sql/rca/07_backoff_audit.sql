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
