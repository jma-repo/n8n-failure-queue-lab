// uses: validate
// Monitor: apply the health rules (silence, failure rate, stuck failures) to the queue API summary.
const health = $input.first().json;
return [{ json: { health, alerts: evaluateHealth(health, $env.LAB_RUNBOOK_URL) } }];
