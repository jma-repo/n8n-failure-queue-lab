"""The four n8n workflows: ingest, retry worker, monitor, and the error handler they all report to."""

from __future__ import annotations

from lab.workflows.nodes import Workflow, alert_tail, code, cond, http_api, http_source, note, schedule, webhook

ERROR_ID = "labError01"
CLASSIFIED = "$('Classify and validate').first().json"


def rows_body(source: str) -> str:
    """Request body for Publish rows: the valid rows of the Classify and validate node."""
    return f"={{{{ JSON.stringify({{ run_id: {CLASSIFIED}.run_id, source: '{source}', rows: {CLASSIFIED}.rows }}) }}}}"


def ingest() -> Workflow:
    w = Workflow("labIngest01", "Lab: ingest FX rates", error_workflow=ERROR_ID)
    note(w, "**Ingest** every 15 minutes, or `POST /webhook/lab-ingest`.\n\nFetch (3 tries for timeouts) → classify and validate in JS → "
         "failures to the DuckDB queue → stage, audit, publish the good rows → log the run → alerts (cool-down kept by the queue API).",
         (0, -1.2), 1480, 130)  # fmt: skip
    tick = schedule(w, "Every 15 minutes", 15, (0, 0.6))
    hook = webhook(w, "Run now (webhook)", "lab-ingest", (0, 1.8))
    start = code(w, "Start run", "start_run", (1, 1), WORKFLOW="ingest")
    fetch = http_source(w, "Fetch source", (2, 1))
    classify = code(w, "Classify and validate", "ingest_classify", (3, 1))
    queue_body = "={{ JSON.stringify({ run_id: $json.run_id, failures: $json.failures }) }}"
    queue = http_api(w, "Queue failures", "POST", "/v1/failures", (4, 1), body=queue_body)
    publish = http_api(w, "Publish rows", "POST", "/v1/batches", (5, 1), body=rows_body("ingest"))
    summary = code(w, "Summarize run", "ingest_summarize", (6, 1))
    log = http_api(w, "Log run", "POST", "/v1/runs", (7, 1), body="={{ JSON.stringify($json.run) }}")
    w.link(tick, start)
    w.link(hook, start)
    w.chain(start, fetch, classify, queue, publish, summary, log)
    alert_tail(w, log, summary, (8, 1))
    return w


def retry() -> Workflow:
    w = Workflow("labRetry01", "Lab: retry failures", error_workflow=ERROR_ID)
    note(w, "**Retry worker** every 5 minutes, or `POST /webhook/lab-retry`.\n\nClaim what is due (a lease keeps two workers off the same item) → "
         "fetch again → re-validate → judge every claimed failure → publish recovered rows → report results (the API picks the next backoff "
         "or dead-letters) → alert on dead letters.", (0, -1.2), 1480, 130)  # fmt: skip
    tick = schedule(w, "Every 5 minutes", 5, (0, 0.6))
    hook = webhook(w, "Run now (webhook)", "lab-retry", (0, 1.8))
    start = code(w, "Start run", "start_run", (1, 1), WORKFLOW="retry")
    claim = http_api(w, "Claim due failures", "POST", "/v1/failures/claim", (2, 1), body='{"limit": 50}')
    due = cond(w, "Anything due?", "={{ $json.items.length }}", "number", "gt", 0, (3, 1))
    idle = w.add("Nothing due", "noOp", 1, {}, (4, 0))
    plan = code(w, "Plan fetch", "retry_plan", (4, 1.6))
    fetch = http_source(w, "Fetch source", (5, 1.6))
    classify = code(w, "Classify and validate", "retry_classify", (6, 1.6))
    publish = http_api(w, "Publish rows", "POST", "/v1/batches", (7, 1.6), body=rows_body("retry"))
    resolve = code(w, "Resolve claimed", "retry_resolve", (1, 3.2))
    new_body = "={{ JSON.stringify({ run_id: $json.run_id, failures: $json.new_failures }) }}"
    queue = http_api(w, "Queue new failures", "POST", "/v1/failures", (2, 3.2), body=new_body)
    res = "$('Resolve claimed').first().json"
    results = f"={{{{ JSON.stringify({{ run_id: {res}.run_id, results: {res}.results }}) }}}}"
    report = http_api(w, "Report results", "POST", "/v1/failures/results", (3, 3.2), body=results)
    summary = code(w, "Summarize run", "retry_summarize", (4, 3.2))
    log = http_api(w, "Log run", "POST", "/v1/runs", (5, 3.2), body="={{ JSON.stringify($json.run) }}")
    w.link(tick, start)
    w.link(hook, start)
    w.chain(start, claim, due)
    w.link(due, plan, 0)
    w.link(due, idle, 1)
    w.chain(plan, fetch, classify, publish, resolve, queue, report, summary, log)
    alert_tail(w, log, summary, (6, 3.2))
    return w


def monitor() -> Workflow:
    w = Workflow("labMonitor01", "Lab: monitor", error_workflow=ERROR_ID)
    note(w, "**Monitor** every 10 minutes. Silence is a failure too: no successful ingest for 45 minutes alerts "
         "even though nothing failed.", (0, -1.2), 1100, 110)  # fmt: skip
    tick = schedule(w, "Every 10 minutes", 10, (0, 0.6))
    hook = webhook(w, "Run now (webhook)", "lab-monitor", (0, 1.8))
    health = http_api(w, "Read health", "GET", "/v1/health/summary", (1, 1))
    rules = code(w, "Evaluate rules", "monitor_evaluate", (2, 1))
    w.link(tick, health)
    w.link(hook, health)
    w.chain(health, rules)
    alert_tail(w, rules, rules, (3, 1))
    return w


def error_handler() -> Workflow:
    w = Workflow(ERROR_ID, "Lab: error handler")
    note(w, "**Error handler** for the other workflows: n8n's own failures (a crashed node, bad credentials) "
         "are queued as `workflow_error` and alerted.", (0, -1.2), 1100, 110)  # fmt: skip
    trigger = w.add("Error Trigger", "errorTrigger", 1, {}, (0, 1))
    fmt = code(w, "Format error", "error_format", (1, 1))
    queue = http_api(w, "Queue failure", "POST", "/v1/failures", (2, 1), body="={{ JSON.stringify($json.failure) }}", fail_open=True)
    w.chain(trigger, fmt, queue)
    alert_tail(w, queue, fmt, (3, 1))
    return w


def all_workflows() -> dict[str, Workflow]:
    """File name -> workflow. The error handler comes first because the others point at its id."""
    return {f"{wf.wid}.json": wf for wf in (error_handler(), ingest(), retry(), monitor())}
