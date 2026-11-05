-- Shared building blocks for the queries that follow. Run first; `uv run lab rca` does.

-- Every ingest run, flagged 0/1 for "did not fully succeed", with the number of the streak of consecutive bad runs it
-- belongs to. This is the classic gaps-and-islands construction: a run starts a streak when it is bad and the one
-- before it was not, and a running sum of those starts numbers the streaks.
CREATE OR REPLACE VIEW v_ingest_runs AS
WITH flagged AS (
    SELECT run_id, started_at, status, source_status, rows_in, rows_failed, (status <> 'success')::INT AS bad
    FROM runs
    WHERE workflow = 'ingest'
),
edges AS (
    SELECT *,
           CASE WHEN bad = 1 AND coalesce(lag(bad) OVER (ORDER BY started_at), 0) = 0 THEN 1 ELSE 0 END AS starts_streak,
           lead(started_at) OVER (ORDER BY started_at) AS next_run_at,
           lead(bad) OVER (ORDER BY started_at) AS next_bad
    FROM flagged
)
SELECT *, CASE WHEN bad = 1 THEN sum(starts_streak) OVER (ORDER BY started_at) END AS streak
FROM edges;

-- One row per streak: when it began, when the last bad run was, and when the first good run after it came.
CREATE OR REPLACE VIEW v_incidents AS
SELECT streak AS incident,
       min(started_at) AS started_at,
       max(started_at) AS last_bad_run_at,
       count(*) AS bad_runs,
       max(CASE WHEN next_bad = 0 THEN next_run_at END) AS recovered_at
FROM v_ingest_runs
WHERE bad = 1
GROUP BY streak;
