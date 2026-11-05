-- Warehouse schema (DuckDB). Every timestamp is UTC, stored without a zone.

CREATE SEQUENCE IF NOT EXISTS seq_event START 1;
CREATE SEQUENCE IF NOT EXISTS seq_alert START 1;
CREATE SEQUENCE IF NOT EXISTS seq_batch START 1;

-- The error taxonomy, loaded from config/error_taxonomy.yaml at startup so SQL can join on it.
CREATE TABLE IF NOT EXISTS error_types (
    error_type   VARCHAR PRIMARY KEY,
    kind         VARCHAR NOT NULL,
    retryable    BOOLEAN NOT NULL,
    max_attempts INTEGER NOT NULL,
    base_s       DOUBLE NOT NULL,
    cap_s        DOUBLE NOT NULL,
    severity     VARCHAR NOT NULL,
    runbook      VARCHAR NOT NULL,
    summary      VARCHAR NOT NULL
);

-- One row per workflow execution that touched the source.
CREATE TABLE IF NOT EXISTS runs (
    run_id           VARCHAR PRIMARY KEY,
    workflow         VARCHAR NOT NULL,   -- ingest | retry | monitor
    trigger_kind     VARCHAR,            -- schedule | webhook | manual
    started_at       TIMESTAMP NOT NULL,
    finished_at      TIMESTAMP,
    status           VARCHAR NOT NULL,   -- success | partial | failed | held
    source_status    INTEGER,            -- HTTP status of the source call, NULL when there was no response
    rows_in          INTEGER DEFAULT 0,  -- cells (date x series) seen in the payload
    rows_ok          INTEGER DEFAULT 0,
    rows_failed      INTEGER DEFAULT 0,
    duration_ms      INTEGER,
    n8n_execution_id VARCHAR,
    note             VARCHAR
);

-- Current state of every failure that was ever queued.
CREATE TABLE IF NOT EXISTS failure_queue (
    failure_id      VARCHAR PRIMARY KEY,
    dedupe_key      VARCHAR NOT NULL,    -- error_type|scope: one problem is one queue item, however often it recurs
    kind            VARCHAR NOT NULL,
    error_type      VARCHAR NOT NULL,
    scope           VARCHAR NOT NULL,    -- source | dataset | SERIES@date | ...
    error_detail    VARCHAR,
    payload         JSON,
    run_id          VARCHAR,             -- the run that first saw it
    status          VARCHAR NOT NULL,    -- pending | in_progress | resolved | dead
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL,
    next_retry_at   TIMESTAMP,
    lease_until     TIMESTAMP,
    first_seen_at   TIMESTAMP NOT NULL,
    last_seen_at    TIMESTAMP NOT NULL,
    last_attempt_at TIMESTAMP,
    resolved_at     TIMESTAMP,
    dead_at         TIMESTAMP,
    occurrences     INTEGER NOT NULL DEFAULT 1
);

-- Append-only ledger: every detection, retry outcome and dead-lettering. The RCA queries read this.
CREATE TABLE IF NOT EXISTS failure_events (
    event_id      BIGINT PRIMARY KEY DEFAULT nextval('seq_event'),
    ts            TIMESTAMP NOT NULL,
    failure_id    VARCHAR,
    run_id        VARCHAR,
    event         VARCHAR NOT NULL,      -- detected | retry_ok | retry_failed | dead_lettered | requeued
    kind          VARCHAR,
    error_type    VARCHAR NOT NULL,
    attempt       INTEGER,               -- 0 on detection, then 1, 2, ...
    http_status   INTEGER,
    detail        VARCHAR,
    retry_in_s    DOUBLE,                -- delay chosen for the next retry
    next_retry_at TIMESTAMP
);

-- Write-audit-publish: rows are staged, audited, then published (visible) or held.
CREATE TABLE IF NOT EXISTS batches (
    batch_id       VARCHAR PRIMARY KEY,
    batch_seq      BIGINT DEFAULT nextval('seq_batch'),  -- creation order; ties in loaded_at must not decide what is visible
    run_id         VARCHAR,
    source         VARCHAR,              -- ingest | retry
    created_at     TIMESTAMP NOT NULL,
    status         VARCHAR NOT NULL,     -- staged | published | held | rolled_back
    rows_new       INTEGER,
    rows_changed   INTEGER,
    rows_unchanged INTEGER,
    audit          JSON,
    decided_at     TIMESTAMP,
    reason         VARCHAR
);

CREATE TABLE IF NOT EXISTS fx_rates_log (
    batch_id  VARCHAR NOT NULL,
    obs_date  DATE NOT NULL,
    series    VARCHAR NOT NULL,
    rate      DOUBLE NOT NULL,
    loaded_at TIMESTAMP NOT NULL
);

-- What consumers read: the newest (by creation order) published version of each (date, series). Rolling a batch back makes the
-- previous version visible again, because nothing is ever overwritten.
CREATE OR REPLACE VIEW fx_rates AS
SELECT l.obs_date, l.series, l.rate, l.batch_id, l.loaded_at
FROM fx_rates_log l
JOIN batches b USING (batch_id)
WHERE b.status = 'published'
QUALIFY row_number() OVER (PARTITION BY l.obs_date, l.series ORDER BY b.batch_seq DESC) = 1;

CREATE TABLE IF NOT EXISTS alerts (
    alert_id          BIGINT PRIMARY KEY DEFAULT nextval('seq_alert'),
    ts                TIMESTAMP NOT NULL,
    alert_key         VARCHAR NOT NULL,
    severity          VARCHAR NOT NULL,
    subject           VARCHAR NOT NULL,
    body              VARCHAR,
    run_id            VARCHAR,
    failure_id        VARCHAR,
    sent              BOOLEAN NOT NULL,
    suppressed_reason VARCHAR
);
