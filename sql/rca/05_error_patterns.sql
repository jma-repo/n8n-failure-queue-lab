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
