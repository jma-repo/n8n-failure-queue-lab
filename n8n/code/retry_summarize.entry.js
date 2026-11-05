// uses: validate
// Retry: one run record and the alerts for failures that just ran out of attempts.
const c = $("Classify and validate").first().json;
const q = $("Queue new failures").first().json;
const rep = $("Report results").first().json;
const p = $("Publish rows").first().json;
const finishedMs = Date.now();
const failureCount = c.validation.dataset_failures.length + c.validation.row_failures.length;
const run = {
  run_id: c.run_id,
  workflow: "retry",
  trigger_kind: c.trigger_kind,
  started_at: c.started_at,
  finished_at: new Date(finishedMs).toISOString(),
  status: runStatus(c.fetch_failure, c.fatal, failureCount, p.outcome),
  source_status: c.source_status,
  rows_in: c.stats.cells,
  rows_ok: c.stats.valid,
  rows_failed: c.stats.invalid,
  duration_ms: finishedMs - c.started_ms,
  n8n_execution_id: String($execution.id),
  note: `claimed ${c.claimed.length}: resolved ${rep.resolved.length}, rescheduled ${rep.rescheduled.length}, dead ${rep.dead.length}`,
};
return [{ json: { run, alerts: deadAlerts([...rep.dead, ...q.dead_on_arrival], $env.LAB_RUNBOOK_URL) } }];
