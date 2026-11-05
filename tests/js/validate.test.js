const test = require("node:test");
const assert = require("node:assert/strict");
const { validatePayload } = require("../../n8n/code/validate.js");
const { CTX, SERIES, observation, payload, failuresOf } = require("./helpers.js");

const run = (body, ctx = CTX) => validatePayload(typeof body === "string" ? body : JSON.stringify(body), ctx);
const damaged = (edit, days) => run(payload(days, (obs) => (edit(obs), obs)));

test("a healthy payload has no failures and every cell is valid", () => {
  const v = run(payload());
  assert.deepEqual([v.fatal, v.dataset_failures, v.row_failures], [false, [], []]);
  assert.equal(v.valid_rows.length, 2 * SERIES.length);
  assert.deepEqual(v.stats, { observations: 2, cells: 16, valid: 16, invalid: 0, duplicates_dropped: 0, unknown_series: [] });
  assert.deepEqual(v.valid_rows[0], { obs_date: "2026-11-02", series: "FXAUDCAD", rate: 0.91 });
});

test("bodies that are not JSON are unparseable and fatal", () => {
  for (const body of ["", "<html>down</html>", '{"observations": [', "null", "42", "\u0000\u0001"]) {
    const v = run(body);
    assert.equal(v.fatal, true, body);
    assert.equal(v.dataset_failures[0].error_type, body === "null" || body === "42" ? "schema_drift" : "unparseable", body);
  }
});

test("a BOM in front of valid JSON is tolerated", () => {
  assert.equal(run("﻿" + JSON.stringify(payload())).valid_rows.length, 16);
});

test("a changed top-level shape is schema drift", () => {
  for (const body of [{ data: [] }, [], { observations: "none" }, { observations: { 0: {} } }]) {
    const v = run(body);
    assert.deepEqual([v.fatal, v.dataset_failures[0].error_type], [true, "schema_drift"], JSON.stringify(body));
  }
});

test("a renamed date key or value key is schema drift, not a pile of row errors", () => {
  const renamedDate = run(payload(undefined, (obs) => obs.map(({ d, ...rest }) => ({ date: d, ...rest }))));
  assert.match(renamedDate.dataset_failures[0].detail, /date key d is missing in 2 of 2 rows/);
  const renamedValue = run(payload(undefined, (obs) => obs.map((o) => Object.fromEntries(Object.entries(o).map(([k, c]) => [k, k === "d" ? c : { value: c.v }])))));
  assert.deepEqual([renamedValue.fatal, renamedValue.dataset_failures[0].error_type], [true, "schema_drift"]);
  assert.match(renamedValue.dataset_failures[0].detail, /found: value/);
  assert.equal(renamedValue.row_failures.length, 0);
});

test("a few odd cells in an otherwise healthy payload are row failures, not drift", () => {
  const v = damaged((obs) => { obs[1].FXEURCAD = { value: "1.57" }; });
  assert.equal(v.fatal, false);
  assert.equal(failuresOf(v, "null_value").length, 1);
});

test("an empty observation list is empty_payload", () => {
  const v = run(payload([]));
  assert.deepEqual([v.fatal, v.dataset_failures[0].error_type], [true, "empty_payload"]);
});

test("a series missing from every row is one dataset failure and the others still load", () => {
  const v = damaged((obs) => obs.forEach((o) => delete o.FXCNYCAD));
  assert.deepEqual(v.dataset_failures.map((f) => [f.error_type, f.scope]), [["missing_series", "series:FXCNYCAD"]]);
  assert.equal(v.fatal, false);
  assert.equal(v.valid_rows.length, 14);
  assert.equal(v.row_failures.length, 0);
});

test("unknown series are ignored but reported", () => {
  const v = damaged((obs) => { obs[0].FXXYZCAD = { v: "9.99" }; });
  assert.deepEqual(v.stats.unknown_series, ["FXXYZCAD"]);
  assert.equal(v.valid_rows.length, 16);
});

test("null, empty, blank and absent values are null_value", () => {
  const v = damaged((obs) => {
    obs[1].FXEURCAD = { v: null };
    obs[1].FXGBPCAD = { v: "" };
    obs[1].FXJPYCAD = { v: "   " };
    obs[1].FXCNYCAD = {};
    delete obs[1].FXAUDCAD;
  });
  assert.deepEqual(failuresOf(v, "null_value").map((f) => f.scope).sort(),
    ["FXAUDCAD@2026-11-03", "FXCNYCAD@2026-11-03", "FXEURCAD@2026-11-03", "FXGBPCAD@2026-11-03", "FXJPYCAD@2026-11-03"]);
  assert.equal(v.valid_rows.length, 16 - 5);
});

test("anything that is not a plain decimal is bad_type", () => {
  for (const raw of ["N/A", "1,3961", "1e3", "-1.4", "1.2.3", "abc", "1.4 CAD", true, [], {}, "NaN", "Infinity", "0x10"]) {
    const v = damaged((obs) => { obs[1].FXUSDCAD = { v: raw }; });
    assert.equal(failuresOf(v, "bad_type").length, 1, JSON.stringify(raw));
  }
});

test("numbers, padded strings and trailing zeros are accepted", () => {
  const v = damaged((obs) => {
    obs[0].FXUSDCAD = { v: 1.4 };
    obs[0].FXEURCAD = { v: " 1.5700 " };
    obs[0].FXGBPCAD = { v: "1.80" };
  });
  assert.equal(v.row_failures.length, 0);
});

test("values outside the plausible range are out_of_range, including zero and a x100 slip", () => {
  for (const raw of ["0.0000", "0", "140.0000", "0.0001", 99999]) {
    const v = damaged((obs) => { obs[1].FXUSDCAD = { v: raw }; });
    assert.equal(failuresOf(v, "out_of_range").length, 1, String(raw));
  }
  assert.equal(damaged((obs) => { obs[1].FXUSDCAD = { v: "2.2" }; }).row_failures.length, 0);
});

test("bad dates are one failure per row, not one per cell", () => {
  for (const d of ["2026-13-45", "2026-11-31", "11/03/2026", "2026-11-1", "", "yesterday", "2026-11-01", "2026-11-20"]) {
    const v = damaged((obs) => { obs[1].d = d; });
    assert.equal(failuresOf(v, "bad_date").length, 1, d);
    assert.equal(v.valid_rows.length, 8, d);
  }
});

test("an identical duplicate row is dropped quietly, a conflicting one rejects both values", () => {
  const same = run(payload(["2026-11-03", "2026-11-03"]));
  assert.deepEqual([same.row_failures.length, same.stats.duplicates_dropped, same.valid_rows.length], [0, 8, 8]);
  const conflict = damaged((obs) => { obs[1].FXUSDCAD = { v: "1.4010" }; }, ["2026-11-03", "2026-11-03"]);
  assert.deepEqual(failuresOf(conflict, "duplicate_conflict").map((f) => f.scope), ["FXUSDCAD@2026-11-03"]);
  assert.equal(conflict.valid_rows.length, 7);
});

test("freshness: four days old is fine, five is stale, and stale is not fatal", () => {
  const tuesday = { today: "2026-11-03" };
  assert.equal(run(payload(["2026-10-30"]), tuesday).dataset_failures.length, 0);
  const v = run(payload(["2026-10-29"]), tuesday);
  assert.deepEqual([v.fatal, v.dataset_failures.map((f) => f.error_type), v.valid_rows.length], [false, ["stale_data"], 8]);
});

test("hostile or odd input never throws", () => {
  const odd = ['{"observations":[null,1,"x",[],{"d":"2026-11-03","__proto__":{"v":1}}]}', '{"observations":[{"d":5}]}', '{"observations":[{"d":null}]}'];
  for (const body of odd) assert.doesNotThrow(() => run(body), body);
  assert.doesNotThrow(() => run({ observations: [observation("2026-11-03", { FXUSDCAD: "1.4" })] }));
});

test("two thousand weekdays validate in well under a second", () => {
  const days = [];
  for (let t = Date.UTC(2018, 0, 1); days.length < 2000; t += 86400000) {
    const day = new Date(t);
    if (day.getUTCDay() % 6 !== 0) days.push(day.toISOString().slice(0, 10));
  }
  const started = Date.now();
  const v = run(payload(days), { today: "2026-11-04", maxAgeDays: 99999 });
  assert.equal(v.valid_rows.length, 2000 * SERIES.length);
  assert.ok(Date.now() - started < 1000);
});

test("the order of the observations does not matter: the real API lists the newest day first", () => {
  const days = ["2026-10-30", "2026-11-02", "2026-11-03"];
  const forward = run(payload(days));
  const backward = run(payload([...days].reverse()));
  assert.deepEqual(backward.valid_rows, forward.valid_rows);
  assert.deepEqual([backward.row_failures, backward.dataset_failures], [forward.row_failures, forward.dataset_failures]);
  const jumbled = run(payload(days, (obs) => [obs[1], obs[2], obs[0]]));
  assert.deepEqual(jumbled.valid_rows, forward.valid_rows);
});
