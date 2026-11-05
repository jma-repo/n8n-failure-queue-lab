# Runbook: FX rates ingest

For whoever receives an alert from this pipeline. It covers what runs where, what healthy looks like, what to do in the
first five minutes, and one playbook per error type. Every command below runs from the repository folder.

## 1. What runs, where

| Piece | What it does | Where to look |
|---|---|---|
| n8n | Runs four workflows: **ingest** (every 15 min), **retry worker** (every 5 min), **monitor** (every 10 min) and an **error handler** | http://localhost:5678 (Executions tab) |
| Queue API + DuckDB | Owns the failure queue, the retry schedule, write-audit-publish batches, the run log and the alert ledger | http://localhost:8000/docs, `uv run lab sql "..."` |
| Source | The Bank of Canada Valet API in live mode; a stand-in with switchable faults in the lab | `uv run lab chaos <fault>` |
| Mailpit | Receives the alert emails in the lab (a real SMTP relay or chat webhook takes its place in production) | http://localhost:8025 |

How a failure moves: the ingest run detects it and writes it to `failure_queue` → the retry worker claims it when it is due
and tries again, waiting twice as long after each failure → it either heals (`resolved`) or runs out of attempts (`dead`),
which sends an alert. Errors that retrying cannot fix are `dead` on arrival.

## 2. What healthy looks like

| Signal | Healthy | Alert rule | Comes from |
|---|---|---|---|
| Freshness | a successful ingest within the last 45 min | `no_success` (critical), from the monitor | `runs` |
| Run failure rate | under 50% of the last hour's runs | `run_failure_rate` (warning, needs 3+ runs) | `runs` |
| Cell failure rate | under 10% of the last hour's cells | `cell_failure_rate` (warning) | `runs` |
| Stuck failures | nothing open for more than 2 hours | `stuck_failure` (warning) | `failure_queue` |
| Dead letters | none older than a working day | `dead:<error_type>` the moment one appears | `failure_queue` |
| Held batch | none | `batch_held` (critical) | `batches` |
| n8n itself | no failed executions | `workflow_error:<workflow>` (critical) | n8n error workflow |

The same alert is sent at most once per cool-down (15 min for critical, 60 min for warning). A muted repeat is still
written to the `alerts` table with `sent = false`, so nothing is hidden.

## 3. When an alert arrives: the first five minutes

1. **Read the subject.** `[lab] 3 failure(s) need a person: schema_drift` names the error type; the body links the playbook.
2. **Get the picture.**
   ```bash
   uv run lab status
   ```
   It prints freshness, failure rates, the queue by state, open failures and any held batch.
3. **Ours or theirs?** Check the source first, then n8n (Executions tab: red rows are failed workflows), then the queue API
   (`curl http://localhost:8000/healthz`). A source problem shows as `http_5xx`, `timeout` or `rate_limited`; a change on
   their side as `schema_drift` or `unparseable`; our own problem as `workflow_error`.
4. **Open the playbook** in section 5 for the error type. Each one says what the system already did on its own.
5. **Write down three times**: when it started, when it was detected, when it was fixed. The post-incident template in
   section 8 needs them, and `sql/rca/04_incidents.sql` can reproduce the first two.

Severity: **critical** means data is missing, wrong or held and someone should act now. **warning** means the system is
retrying and a person only needs to look if it does not heal.

## 4. Tools and queries

```bash
uv run lab status                                  # health, open failures, held batches
uv run lab sql "SELECT ..."                        # one read-only SELECT against the live warehouse
uv run lab requeue <failure_id>                    # or: uv run lab requeue --dead
uv run lab approve <batch_id>                      # publish a batch the audit held
uv run lab rollback <batch_id> --reason "..."      # hide a published batch
uv run lab trigger ingest|retry|monitor            # run a workflow now
uv run lab chaos http_500 --first-n 2              # (lab only) make the source misbehave; `uv run lab chaos ok` ends it
```

Queries worth having at hand:

```sql
-- what is open right now, oldest first
SELECT failure_id, error_type, scope, status, attempts, max_attempts, next_retry_at, error_detail
FROM failure_queue WHERE status IN ('pending', 'in_progress', 'dead') ORDER BY first_seen_at;

-- the full story of one failure: when it was seen, every retry, the wait chosen after each
SELECT ts, event, attempt, error_type, http_status, retry_in_s, detail
FROM failure_events WHERE failure_id = '<failure_id>' ORDER BY event_id;

-- the last 20 runs
SELECT started_at, workflow, status, source_status, rows_in, rows_failed, note
FROM runs ORDER BY started_at DESC LIMIT 20;

-- held and rolled-back batches, and why
SELECT batch_id, created_at, status, rows_new, rows_changed, reason FROM batches WHERE status IN ('held', 'rolled_back');

-- what the consumers see right now for one series
SELECT obs_date, rate, batch_id FROM fx_rates WHERE series = 'FXUSDCAD' ORDER BY obs_date DESC LIMIT 5;

-- every alert, including the ones the cool-down muted
SELECT ts, alert_key, severity, sent, subject, suppressed_reason FROM alerts ORDER BY ts DESC LIMIT 20;
```

For trends and post-mortems use the queries in `sql/rca/` (see `docs/rca-report.md`).

## 5. Playbooks by error type

Waits below are the exponential schedule before jitter (equal jitter keeps between half and all of each wait). The numbers
live in `config/error_taxonomy.yaml`; change them there, not here.

<a id="http-5xx"></a>
### http_5xx: the source answers with a 5xx status
- **Confirm:** `curl -i "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"` (or the stand-in on :8080). The failure detail shows the status.
- **Already done:** 6 retries, waiting 1, 2, 4, 8, 16, 32 minutes; dead-lettered after about 30 to 60 minutes of continuous failure, which sends the alert.
- **Do:** if the source status page confirms an incident, wait; nothing is lost because each run re-reads the last five days. If the source is fine, look at n8n for a changed URL or proxy.
- **Close:** after the source recovers run `uv run lab requeue --dead`. The next retry loads the missing rows; check `fx_rates`.

<a id="timeout"></a>
### timeout: the source did not answer within 10 seconds
- **Confirm:** `curl -m 10 -w "%{time_total}\n" -o /dev/null -s <source url>`. A slow window that repeats at the same hour is a schedule on their side: `sql/rca/02_failure_rate_by_hour.sql` marks it RECURRING.
- **Already done:** the HTTP node tried 3 times (2 s apart) before the failure was queued, then 6 retries like `http_5xx`.
- **Do:** a recurring window needs no action but a longer timeout or a quieter schedule (move the ingest off that hour). A one-off: treat as `http_5xx`.
- **Close:** as `http_5xx`.

<a id="network"></a>
### network: the connection failed (DNS, refused, reset)
- **Confirm:** from the n8n container: `docker compose exec n8n wget -qO- <source url>`. Failing here but not on your machine means a container network or DNS problem.
- **Already done:** 3 tries in the flow, then 6 retries like `http_5xx`.
- **Do:** `docker compose restart n8n` clears most stale DNS; check the host's proxy or VPN.
- **Close:** requeue dead items once `docker compose exec n8n wget` works.

<a id="rate-limited"></a>
### rate_limited: the source answered 429
- **Confirm:** the failure detail has the status; the queue honours `Retry-After` when the source sends it.
- **Already done:** 8 retries from 2 minutes up to a 2-hour cap, never sooner than `Retry-After`.
- **Do:** check that nothing else calls the same source, and that the schedule is not faster than needed (the source publishes once a day; hourly would do). Bank of Canada terms forbid circumventing its request limits.
- **Close:** it heals by itself; if it dead-letters, slow the schedule first, then requeue.

<a id="http-4xx"></a>
### http_4xx: the source rejected the request
- **Meaning:** 400, 401, 403, 404 or 410. Retrying will not help, so it is dead-lettered on arrival (critical).
- **Confirm:** open the URL from `$env.LAB_SOURCE_BASE` in a browser. 404 usually means the endpoint moved; 401 and 403 mean access changed.
- **Do:** fix the URL or the credential in n8n (`LAB_SOURCE_BASE`, the HTTP node), then `uv run lab bootstrap` if the workflow changed.
- **Close:** `uv run lab requeue --dead`.

<a id="unparseable"></a>
### unparseable: the body is not valid JSON
- **Confirm:** `curl -s <source url> | head -c 300`. An HTML page ("service unavailable") with status 200 is the classic cause; a cut-off JSON body is a truncated download.
- **Already done:** 3 retries after 5, 10, 20 minutes (these are usually transient).
- **Do:** if it persists, the source changed format or an intermediary (proxy, captive portal) is answering. Fix the cause or switch the HTTP node to another endpoint.
- **Close:** requeue.

<a id="schema-drift"></a>
### schema_drift: the payload shape changed
- **Meaning:** the observations list, the `d` key or the `v` key is gone in most of the payload (the validator names which). Nothing was loaded; dead-lettered on arrival (critical).
- **Confirm:** `curl -s <source url> | head -c 600` and compare with `src/lab/source_sim/data.py` (`build_payload`).
- **Do:** update `n8n/code/validate.js` (and its tests in `tests/js/`), run `uv run python scripts/build_workflows.py`, then `uv run lab bootstrap`. Do not loosen the check without a test: a tolerant reader that guesses is how wrong rates get published.
- **Close:** `uv run lab requeue --dead`; the retry re-fetches and loads the missing days.

<a id="empty-payload"></a>
### empty_payload: valid response, no observations
- **Confirm:** the body parses but `observations` is `[]`. It happens right at a publication boundary or after an upstream reset.
- **Already done:** 4 retries after 5, 10, 20, 40 minutes.
- **Do:** if still empty after the last retry, check the source status page and the query parameters (`recent`, `start_date`).
- **Close:** requeue after the source returns data.

<a id="missing-series"></a>
### missing_series: an expected series is absent from the whole payload
- **Confirm:** the detail names the series. The other series were loaded normally.
- **Already done:** 4 retries after 10, 20, 40, 80 minutes.
- **Do:** if a series was discontinued upstream, remove it from `LAB_SERIES` in `docker-compose.yml` (and from `RANGES` in `validate.js`); otherwise wait for the source.
- **Close:** requeue, or remove the series and close the item by requeueing once.

<a id="stale-data"></a>
### stale_data: the newest observation is older than four days
- **Meaning:** the rows that arrived were loaded, but nothing newer has been published. The limit is four calendar days so a long weekend does not alarm; the Bank publishes around 16:30 Eastern on business days.
- **Already done:** 8 retries from 15 minutes up to 2 hours between tries.
- **Do:** check whether the source is simply late (a holiday), and whether the monitor's `no_success` alert is also firing.
- **Close:** heals when a new day appears.

<a id="null-value"></a>
### null_value: a value is null, empty or absent in one row
- **Meaning:** one cell (a series on a date) is unusable. The rest of the batch was published (the run is `partial`); only this cell waits.
- **Confirm:** `uv run lab sql "SELECT scope, error_detail, attempts FROM failure_queue WHERE error_type = 'null_value' AND status <> 'resolved'"`.
- **Already done:** 4 retries after 5, 10, 20, 40 minutes, re-reading the source each time in case it was corrected.
- **Do:** if the source never fixes it, decide with the data owner whether the gap is acceptable. Do not fill it by hand in the warehouse.
- **Close:** requeue once the source is corrected; or leave it dead and note the gap in the post-incident record.

<a id="bad-type"></a>
### bad_type: a value is not a plain decimal number
- **Meaning:** `N/A`, `1,39` (comma decimal), `1e3`, a boolean. The validator is strict on purpose: a comma decimal read as `139` would be a silent 100x error.
- **Already done:** 3 retries after 5, 10, 20 minutes.
- **Do:** if the source switched to a locale format, the fix belongs in `validate.js` with a test for the new format, not in a looser regex.
- **Close:** requeue after the fix is deployed.

<a id="out-of-range"></a>
### out_of_range: a number is outside the plausible range for its series
- **Meaning:** zero or off by orders of magnitude (critical, because a wrong rate is worse than a missing one).
- **Confirm:** the detail shows the value and the range from `RANGES` in `n8n/code/validate.js`.
- **Already done:** the cell was not published. 3 retries after 5, 10, 20 minutes.
- **Do:** compare with another source. If the rate is real (a currency re-denominated, a genuine crash), widen the range in `validate.js` with a test and deploy; otherwise wait for the correction.
- **Close:** requeue.

<a id="bad-date"></a>
### bad_date: an observation date is malformed, in the future or on a weekend
- **Meaning:** the whole row is skipped (one failure per row, not per cell). Not retryable: a date does not fix itself, so it is dead-lettered on arrival.
- **Do:** look at the raw row in `failure_queue.payload` and report it upstream. If the source now publishes weekends, change the rule in `validate.js` with a test.
- **Close:** requeue after a fix; otherwise note it and leave it dead.

<a id="duplicate-conflict"></a>
### duplicate_conflict: the same series and date appear twice with different values
- **Meaning:** both values were rejected because there is no way to know which is right. Identical duplicates are dropped silently and are not failures.
- **Already done:** 3 retries after 5, 10, 20 minutes.
- **Do:** fetch the source by hand and see whether the duplication persists. If it does, ask the source which value wins before adding a rule.
- **Close:** requeue.

<a id="audit-jump"></a>
### audit_jump: the batch was held before publishing
- **Meaning:** every row passed validation, but more than a quarter of the checked rates moved more than 5% from the previous observation. Nothing was published and consumers still see the previous values (critical).
- **Confirm:** `uv run lab sql "SELECT batch_id, reason, audit FROM batches WHERE status = 'held' ORDER BY created_at DESC"`. The alert lists the worst moves.
- **Do:** compare the numbers with an independent source. **Real move** (a genuine market event): `uv run lab approve <batch_id>`. **Bad data**: leave it held; the failure gets 3 retries after 30, 60, 120 minutes and heals when the source corrects itself.
- **Close:** if you approved, the audit failure resolves on the next retry (nothing new is left to publish).

<a id="workflow-error"></a>
### workflow_error: n8n itself failed
- **Meaning:** a node crashed or a credential broke and the error handler caught it. It is dead-lettered on arrival (critical).
- **Confirm:** n8n, Executions tab: open the red execution; the failing node is highlighted with its message. If the queue API was down, the alert still arrived, because alerts fail open when the queue API cannot be reached.
- **Do:** fix the node or restore the dependency (`docker compose ps`, `docker compose logs queue-api`). Workflows are code: `git diff n8n/` shows what changed; `git revert` and `uv run lab bootstrap` roll a bad change back.
- **Close:** run the workflow once (`uv run lab trigger ingest`), confirm a `success` run, then requeue the `workflow_error` item.

## 6. Rollback and replay

- **Held versus rolled back.** The audit *holds* a suspicious batch before it is ever visible (`status = held`). A person can *roll back* a batch that is already published (`status = rolled_back`). Both leave the rows in `fx_rates_log`; the `fx_rates` view shows the newest published version, so a rollback makes the previous one visible again.
- **Roll back a published batch:** `uv run lab rollback <batch_id> --reason "restated upstream, see ticket"`, then check with the `fx_rates` query in section 4.
- **Approve a held batch:** `uv run lab approve <batch_id>`.
- **Replay dead letters** after the cause is fixed: `uv run lab requeue --dead` (or one id). Each gets a fresh retry budget and the ledger records `requeued`.
- **Roll back a workflow change:** `git revert <commit>`, `uv run python scripts/build_workflows.py`, `uv run lab bootstrap`. Version control of workflows is the free alternative to n8n's paid Environments feature.

## 7. Maintenance

- **Change retry behaviour:** edit `config/error_taxonomy.yaml` (budget, base, cap, severity, retryable) and restart the queue API (`docker compose restart queue-api`). `uv run pytest tests/test_runbook.py` checks that every error type still has a playbook here.
- **Back up the warehouse:** stop the queue API, copy `lab.duckdb` out of the `lab-data` volume, start it again. DuckDB allows one writer, so do not copy the file while the API runs.
- **Alert hygiene:** if one alert key keeps muting itself, the cause is still open: fix or close it. Review `SELECT alert_key, count(*) FROM alerts GROUP BY 1` monthly.
- **Time scale:** `LAB_TIME_SCALE` shrinks every delay and cool-down (0.01 turns minutes into seconds). Keep it at 1 anywhere real.

## 8. After the incident

Fill in this template and keep it with the ticket. `uv run lab history && uv run lab rca` shows how each answer is found from the ledger.

| | |
|---|---|
| Summary | one sentence: what broke and who was affected |
| Timeline (UTC) | started, detected, alerted, mitigated, resolved |
| Impact | rows late or missing, series affected, consumers who saw wrong numbers (should be none) |
| Root cause | what changed or failed, with the evidence (a query or a log line) |
| Detection | how it was noticed and how long that took (`minutes_to_alert` in `sql/rca/04_incidents.sql`) |
| What worked | retries, the audit, a rollback, the alert text |
| Action items | an owner and a date each; prefer a test or a rule over a reminder |

## 9. Known limits

- The stand-in source and the two weeks of history are simulated. Live mode talks to the real Bank of Canada Valet API (see the README).
- Alerts go to a local SMTP sink. In production point the n8n Send Email node at a real relay, or swap it for a chat webhook.
- One queue API instance and one DuckDB file: a single writer and no high availability. Fine for this volume; it is the first thing to replace under real load.
