# Design decisions

The choices worth defending in a review, with what was traded away.

## A queue API in front of DuckDB, not n8n talking to DuckDB directly
DuckDB is embedded and allows one writer; n8n has no DuckDB node and its Code nodes cannot load native modules safely. One small
service owns the file. **Gain:** the queue logic (what is due, the backoff, the lease, the audit) is ordinary Python and SQL with a
fake clock, so unit tests pin it down instead of hoping a workflow does the right thing. **Cost:** one more container and an HTTP
hop; a single writer is also a single point of failure (see "alerts fail open").

## Validation in n8n (JavaScript), the rules in one file
The workflow is where a reviewer expects to see validation, and the Code node is the idiomatic place. To keep that from becoming an
untested blob, the JavaScript lives in `n8n/code/validate.js`, is embedded into the nodes by a build script, and is tested with
Node's own test runner (plus a compile check of every generated Code node). The error types it can raise are checked against
`config/error_taxonomy.yaml` by a Python test. **Cost:** the library is embedded in several nodes, so the workflow JSON is large;
it is generated, never edited.

## Generated workflow JSON
Hand-editing n8n's JSON is error-prone and unreviewable. `src/lab/workflows` builds it with stable ids, so a change is a small diff,
and a test fails if the committed files differ from what the definitions build. This is also the free answer to n8n's paid
Environments feature: workflows are code in git, and `lab bootstrap` deploys them.

## One queue item per error type and scope
A failing source seen by 68 consecutive runs should not become 68 rows to retry. Keying by `error_type|scope` makes it one item with
an occurrence count, and the ledger (`failure_events`) still records every detection, which is what the failure-rate queries use.
**Cost:** a failure that changes shape while it is being retried keeps its original type on the queue row; the ledger records what
each retry actually saw.

## Retry budgets per error type, and "not retryable" as a first-class answer
A 5xx deserves patience, a schema change deserves none. Retrying the second kind only delays the alert, so it is dead-lettered on
arrival. Budgets, base waits, caps and severity are data (`config/error_taxonomy.yaml`), shared by the API, the SQL and the runbook,
and tests keep the three in step.

## Equal jitter
Half of each exponential wait is kept and half is randomized. Without jitter, failures that start together retry together and hit a
recovering source in a wave. Retry-After is a floor, never a replacement.

## Write, audit, publish
Row-level validation cannot catch numbers that are individually plausible but collectively wrong (every rate up 10% because of an
upstream restatement). Rows are staged, a window function compares each with the previous observation, and the batch is held if too
many moved. Nothing is ever overwritten: the `fx_rates` view picks the newest published version, so a rollback is a status change.
**Cost:** a genuine large move is held until a person approves it, which is the intended trade for a finance feed.

## Alerts: cool-down, fail open, and silence
Alerts are muted per key for a cool-down so one outage is one email, not forty. They fail open: if the queue API cannot be reached to
ask whether to send, n8n sends anyway, because a lost alert is worse than a duplicate. The monitor treats a long absence of success
as a failure of its own, since nothing "fails" when a schedule stops.

## Simulated history for the SQL analysis
Real incidents cannot be ordered up for a portfolio. The history replays scripted faults through the real store, and the RCA queries
are tested blind against the script. The report says plainly that it is simulated. **Cost:** the numbers show the method, not the
behaviour of any real source.

## What would change first under load
The single DuckDB writer and the polling retry worker. A queue table in Postgres with `SELECT ... FOR UPDATE SKIP LOCKED`, or a real
broker, would replace the claim logic; the API contract and the workflows would not need to change.
