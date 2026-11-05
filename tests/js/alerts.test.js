const test = require("node:test");
const assert = require("node:assert/strict");
const { deadAlerts, heldAlert, evaluateHealth, errorAlert } = require("../../n8n/code/validate.js");

const URL = "https://example.test/runbook.md";
const dead = (type, scope, extra = {}) => ({ error_type: type, scope, detail: "boom", attempts: 6, severity: "warning", runbook: type.replace("_", "-"), ...extra });

test("dead failures are grouped by error type, one alert per type, worst severity wins", () => {
  const alerts = deadAlerts([dead("http_5xx", "source"), dead("null_value", "A@1"), dead("null_value", "B@1", { severity: "critical" })], URL);
  assert.deepEqual(alerts.map((a) => [a.key, a.severity]), [["dead:http_5xx", "warning"], ["dead:null_value", "critical"]]);
  assert.match(alerts[1].subject, /2 failure\(s\) need a person: null_value/);
  assert.match(alerts[1].body, /- A@1: boom \(attempts: 6\)/);
  assert.match(alerts[1].body, /Runbook: https:\/\/example.test\/runbook.md#null-value/);
});

test("a long list is cut to ten lines and says how many were left out", () => {
  const many = Array.from({ length: 14 }, (_, i) => dead("bad_type", `S${i}@2026-11-03`));
  const [alert] = deadAlerts(many, URL);
  assert.equal(alert.body.split("\n").filter((l) => l.startsWith("- ")).length, 10);
  assert.match(alert.body, /and 4 more/);
});

test("no dead failures, no alerts", () => assert.deepEqual(deadAlerts([], URL), []));

test("a held batch alert names the batch, the worst moves and the way out", () => {
  const a = heldAlert({ batch_id: "b-1", audit: { summary: "8 of 8 rates moved", worst: [{ series: "FXUSDCAD", obs_date: "2026-11-04", move_pct: 10.01 }] } }, URL);
  assert.equal(a.key, "batch_held");
  assert.match(a.body, /FXUSDCAD on 2026-11-04: 10.01%/);
  assert.match(a.body, /approve/);
});

const healthy = () => ({ last_success_at: "t", minutes_since_success: 5, runs_1h: 4, bad_runs_1h: 0, run_failure_rate_1h: 0, cell_failure_rate_1h: 0, queue: { pending: 0, in_progress: 0, dead: 0, resolved: 0 }, due_now: 0, oldest_open_minutes: null });

test("a healthy system raises nothing", () => assert.deepEqual(evaluateHealth(healthy(), URL), []));

test("silence is an alert: no success for 45 minutes", () => {
  const keys = (h) => evaluateHealth(h, URL).map((a) => a.key);
  assert.deepEqual(keys({ ...healthy(), minutes_since_success: 46 }), ["no_success"]);
  assert.deepEqual(keys({ ...healthy(), minutes_since_success: 45 }), []);
  assert.deepEqual(keys({ ...healthy(), minutes_since_success: null, last_success_at: null, runs_1h: 0 }), []);
  assert.deepEqual(keys({ ...healthy(), minutes_since_success: null, last_success_at: null, runs_1h: 2 }), ["no_success"]);
});

test("failure-rate and stuck-failure rules need enough evidence", () => {
  const keys = (h) => evaluateHealth(h, URL).map((a) => a.key);
  assert.deepEqual(keys({ ...healthy(), runs_1h: 2, bad_runs_1h: 2, run_failure_rate_1h: 1 }), []);
  assert.deepEqual(keys({ ...healthy(), runs_1h: 4, bad_runs_1h: 2, run_failure_rate_1h: 0.5 }), ["run_failure_rate"]);
  assert.deepEqual(keys({ ...healthy(), cell_failure_rate_1h: 0.1 }), ["cell_failure_rate"]);
  assert.deepEqual(keys({ ...healthy(), oldest_open_minutes: 121 }), ["stuck_failure"]);
  assert.deepEqual(keys({ ...healthy(), oldest_open_minutes: 119 }), []);
});

test("the error workflow turns an n8n error into an alert keyed by workflow", () => {
  const a = errorAlert({ workflow: { name: "Lab: ingest" }, execution: { lastNodeExecuted: "Fetch source", error: { message: "boom" }, url: "http://x/1" } }, URL);
  assert.equal(a.key, "workflow_error:Lab: ingest");
  assert.match(a.body, /stopped at node "Fetch source"/);
  assert.match(errorAlert({}, URL).subject, /unknown workflow/);
});
