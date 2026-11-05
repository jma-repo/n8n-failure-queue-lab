const test = require("node:test");
const assert = require("node:assert/strict");
const { validatePayload, resolveClaimed, newFailures, planWindow, runStatus, classifyFetch } = require("../../n8n/code/validate.js");
const { CTX, payload } = require("./helpers.js");

const validate = (body) => validatePayload(JSON.stringify(body), CTX);
const claimed = (kind, error_type, scope, id = scope) => ({ failure_id: id, kind, error_type, scope });
const healthy = () => validate(payload());

test("a retry whose fetch failed again fails every claimed item with that error", () => {
  const fetchFailure = classifyFetch({ statusCode: 503 });
  const out = resolveClaimed([claimed("fetch", "http_5xx", "source", "a"), claimed("row", "null_value", "FXUSDCAD@2026-11-03", "b")], healthy(), fetchFailure);
  assert.deepEqual(out.map((r) => [r.failure_id, r.ok, r.error_type, r.http_status]), [["a", false, "http_5xx", 503], ["b", false, "http_5xx", 503]]);
});

test("a fetch failure is fixed as soon as the source answers", () => {
  assert.deepEqual(resolveClaimed([claimed("fetch", "timeout", "source", "a")], healthy(), null), [{ failure_id: "a", ok: true, detail: "the source answered" }]);
});

test("a dataset failure is fixed only when that problem is gone", () => {
  const stale = validate(payload(["2026-10-20"]));
  const item = claimed("dataset", "stale_data", "dataset", "s");
  assert.equal(resolveClaimed([item], stale, null)[0].ok, false);
  assert.equal(resolveClaimed([item], healthy(), null)[0].ok, true);
  const drift = validatePayload("{}", CTX);
  assert.deepEqual(resolveClaimed([item], drift, null)[0].error_type, "schema_drift");
});

test("a row failure is fixed when the cell is valid now, and reports what is still wrong otherwise", () => {
  const fixed = claimed("row", "null_value", "FXEURCAD@2026-11-03", "f");
  const still = claimed("row", "null_value", "FXGBPCAD@2026-11-03", "g");
  const gone = claimed("row", "null_value", "FXJPYCAD@2020-01-01", "h");
  const v = validate(payload(undefined, (obs) => { obs[1].FXGBPCAD = { v: "N/A" }; return obs; }));
  const [a, b, c] = resolveClaimed([fixed, still, gone], v, null);
  assert.deepEqual([a.ok, b.ok, b.error_type, c.ok], [true, false, "bad_type", false]);
  assert.match(c.detail, /not in the latest payload/);
});

test("when the whole payload is unusable a row failure reports the dataset problem", () => {
  const out = resolveClaimed([claimed("row", "null_value", "FXEURCAD@2026-11-03")], validatePayload("<html>", CTX), null);
  assert.deepEqual([out[0].ok, out[0].error_type], [false, "unparseable"]);
});

test("an audit failure is fixed when the retried batch is not held again", () => {
  const item = claimed("audit", "audit_jump", "batch", "x");
  assert.equal(resolveClaimed([item], healthy(), null, "published")[0].ok, true);
  assert.deepEqual(resolveClaimed([item], healthy(), null, "held").map((r) => [r.ok, r.error_type]), [[false, "audit_jump"]]);
});

test("a kind that is never retried automatically is not reported as fixed", () => {
  assert.equal(resolveClaimed([claimed("system", "workflow_error", "wf")], healthy(), null)[0].ok, false);
});

test("problems already tracked by a claimed failure are not queued a second time", () => {
  const v = validate(payload(undefined, (obs) => { obs[1].FXEURCAD = { v: "N/A" }; obs[1].FXGBPCAD = { v: null }; return obs; }));
  const tracked = [claimed("row", "null_value", "FXEURCAD@2026-11-03")]; // same cell, now a different error type
  assert.deepEqual(newFailures(v, null, tracked).map((f) => f.scope), ["FXGBPCAD@2026-11-03"]);
  const stale = validate(payload(["2026-10-20"]));
  assert.equal(newFailures(stale, null, [claimed("dataset", "stale_data", "dataset")]).length, 0);
  assert.equal(newFailures(stale, null, [claimed("dataset", "schema_drift", "dataset")]).length, 1);
  assert.deepEqual(newFailures(healthy(), { error_type: "timeout", detail: "t" }, []).map((f) => f.scope), ["source"]);
});

test("the retry fetch starts at the oldest date a claimed row failure is about", () => {
  const items = [claimed("row", "null_value", "FXEURCAD@2026-11-03"), claimed("row", "null_value", "FXGBPCAD@2026-10-29"), claimed("fetch", "timeout", "source"), claimed("row", "bad_date", "date:oops")];
  assert.deepEqual(planWindow(items), { start_date: "2026-10-29" });
  assert.deepEqual(planWindow([claimed("fetch", "timeout", "source")]), { start_date: null });
});

test("run status: failed beats held beats partial beats success", () => {
  assert.equal(runStatus(null, false, 0, "published"), "success");
  assert.equal(runStatus(null, false, 3, "published"), "partial");
  assert.equal(runStatus(null, false, 0, "held"), "held");
  assert.equal(runStatus(null, true, 1, "noop"), "failed");
  assert.equal(runStatus({ error_type: "timeout" }, false, 1, "noop"), "failed");
});
