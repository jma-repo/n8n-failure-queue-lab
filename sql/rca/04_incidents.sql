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
