const { RANGES } = require("../../n8n/code/validate.js");

const SERIES = Object.keys(RANGES);
const BASE = { FXUSDCAD: 1.4, FXEURCAD: 1.57, FXGBPCAD: 1.8, FXJPYCAD: 0.0092, FXCNYCAD: 0.209, FXAUDCAD: 0.91, FXCHFCAD: 1.58, FXMXNCAD: 0.075 };
const CTX = { today: "2026-11-04" }; // a Wednesday

function observation(d, edit = {}) {
  const o = { d };
  for (const s of SERIES) o[s] = { v: BASE[s].toFixed(4) };
  return { ...o, ...edit };
}

// a healthy Valet-shaped body for the given days; edit(observations) may damage it
function payload(days = ["2026-11-02", "2026-11-03"], edit = (obs) => obs) {
  return { terms: { url: "https://example.test/terms" }, observations: edit(days.map((d) => observation(d))) };
}

const failuresOf = (v, type) => v.row_failures.filter((f) => f.error_type === type);

module.exports = { SERIES, BASE, CTX, observation, payload, failuresOf };
