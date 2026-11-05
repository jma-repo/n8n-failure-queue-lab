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
