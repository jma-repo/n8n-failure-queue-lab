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
