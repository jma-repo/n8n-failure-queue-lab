// uses: validate
// Error workflow: n8n's own failures become a dead-lettered queue item and an alert.
const err = $input.first().json;
const alert = errorAlert(err, $env.LAB_RUNBOOK_URL);
const toQueue = { run_id: `error-${Date.now()}`, failures: [{ error_type: "workflow_error", scope: alert.wf, detail: alert.message, payload: { execution: err.execution || null } }] };
return [{ json: { alerts: [alert], failure: toQueue } }];
