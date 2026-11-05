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
