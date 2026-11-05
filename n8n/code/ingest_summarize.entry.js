// uses: validate
// Ingest: one run record and the alerts worth sending, from what the three API calls answered.
const c = $("Classify and validate").first().json;
const q = $("Queue failures").first().json;
const p = $("Publish rows").first().json;
const finishedMs = Date.now();
const runbook = $env.LAB_RUNBOOK_URL;
const run = {
  run_id: c.run_id,
  workflow: "ingest",
  trigger_kind: c.trigger_kind,
  started_at: c.started_at,
  finished_at: new Date(finishedMs).toISOString(),
  status: runStatus(c.fetch_failure, c.fatal, c.failures.length, p.outcome),
  source_status: c.source_status,
  rows_in: c.stats.cells,
  rows_ok: c.stats.valid,
  rows_failed: c.stats.invalid,
  duration_ms: finishedMs - c.started_ms,
  n8n_execution_id: String($execution.id),
  note: `new ${p.rows_new || 0}, changed ${p.rows_changed || 0}, unchanged ${p.rows_unchanged || 0}; queued ${q.created.length}, repeated ${q.repeated}; batch ${p.outcome}`,
};
const alerts = deadAlerts(q.dead_on_arrival, runbook);
if (p.outcome === "held") alerts.push({ ...heldAlert(p, runbook), run_id: c.run_id });
return [{ json: { run, alerts } }];
