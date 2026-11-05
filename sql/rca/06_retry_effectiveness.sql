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
