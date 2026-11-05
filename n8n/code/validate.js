// Shared by the ingest and retry workflows. In n8n the build script embeds this file in a Code node together with a
// small entry point (n8n/code/*.entry.js); the tests load it with require().
//
// classifyFetch  : what happened to the HTTP request (timeout, 5xx, 429 ...) or null when it succeeded
// validatePayload: what is wrong with a body that arrived, cell by cell and as a whole
// resolveClaimed : given the failures a retry claimed and a fresh validation, which ones are fixed

const RANGES = {
  FXUSDCAD: [0.8, 2.2],
  FXEURCAD: [0.9, 2.5],
  FXGBPCAD: [1.0, 2.8],
  FXJPYCAD: [0.004, 0.02],
  FXCNYCAD: [0.1, 0.4],
  FXAUDCAD: [0.5, 1.4],
  FXCHFCAD: [0.9, 2.4],
  FXMXNCAD: [0.03, 0.15],
};
const FATAL = new Set(["schema_drift", "unparseable", "empty_payload"]);
const DAY_MS = 86400000;

function ctxWithDefaults(ctx = {}) {
  return {
    series: Object.keys(RANGES),
    ranges: RANGES,
    maxAgeDays: 4,
    futureToleranceDays: 1,
    today: new Date().toISOString().slice(0, 10),
    ...ctx,
  };
}

function parseDay(text) {
  if (typeof text !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
  const [y, m, d] = text.split("-").map(Number);
  const ms = Date.UTC(y, m - 1, d);
  const back = new Date(ms);
  const real = back.getUTCFullYear() === y && back.getUTCMonth() === m - 1 && back.getUTCDate() === d;
  return real ? ms : null;
}

function parseRetryAfter(value) {
  if (value === undefined || value === null) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.min(Math.max(seconds, 0), 3600);
  const at = Date.parse(String(value));
  return Number.isNaN(at) ? null : Math.min(Math.max((at - Date.now()) / 1000, 0), 3600);
}

function failure(error_type, detail, extra = {}) {
  return { error_type, detail: String(detail).slice(0, 300), ...extra };
}

function classifyFetch(resp) {
  if (resp === undefined || resp === null) return failure("network", "the HTTP node returned nothing");
  if (resp.error !== undefined) {
    const text = typeof resp.error === "string" ? resp.error : resp.error.message || JSON.stringify(resp.error);
    return failure(/timeout|timed out|etimedout|econnaborted/i.test(text) ? "timeout" : "network", text);
  }
  const status = Number(resp.statusCode);
  if (status >= 200 && status < 300) return null;
  const headers = resp.headers || {};
  const retryAfter = parseRetryAfter(headers["retry-after"] ?? headers["Retry-After"]);
  const detail = `HTTP ${status} ${resp.statusMessage || ""}`.trim();
  if (status === 429) return failure("rate_limited", detail, { http_status: status, retry_after_s: retryAfter });
  if (status === 408) return failure("timeout", detail, { http_status: status });
  if (status >= 500) return failure("http_5xx", detail, { http_status: status, retry_after_s: retryAfter });
  return failure("http_4xx", detail, { http_status: status });
}

function parseBody(raw) {
  if (raw !== null && typeof raw === "object") return { ok: true, value: raw };
  const text = String(raw ?? "").replace(/^﻿/, "").trim();
  if (!text) return { ok: false, detail: "the body is empty" };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (err) {
    return { ok: false, detail: `${err.message}; the body starts with: ${text.slice(0, 60)}` };
  }
}


const isObject = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

function validatePayload(raw, ctxIn) {
  const ctx = ctxWithDefaults(ctxIn);
  const out = {
    fatal: false,
    dataset_failures: [],
    row_failures: [],
    valid_rows: [],
    stats: { observations: 0, cells: 0, valid: 0, invalid: 0, duplicates_dropped: 0, unknown_series: [] },
  };
  const dataset = (type, detail, scope = "dataset") => {
    out.dataset_failures.push(failure(type, detail, { scope }));
    if (FATAL.has(type)) out.fatal = true;
  };
  const parsed = parseBody(raw);
  if (!parsed.ok) return dataset("unparseable", parsed.detail), out;
  const root = parsed.value;
  if (!isObject(root) || !Array.isArray(root.observations)) {
    const keys = isObject(root) ? Object.keys(root).join(", ") : typeof root;
    return dataset("schema_drift", `no observations list; top-level keys: ${keys || "none"}`), out;
  }
  const obs = root.observations;
  out.stats.observations = obs.length;
  if (obs.length === 0) return dataset("empty_payload", "observations is an empty list"), out;
  const rows = obs.filter(isObject);
  const dated = rows.filter((o) => "d" in o);
  if (dated.length < obs.length / 2) {
    const keys = [...new Set(rows.flatMap((o) => Object.keys(o)))].slice(0, 8).join(", ");
    return dataset("schema_drift", `the date key d is missing in ${obs.length - dated.length} of ${obs.length} rows; keys: ${keys}`), out;
  }
  let cellsSeen = 0;
  let cellsWithoutV = 0;
  let oddKeys = "";
  for (const o of dated) {
    for (const sid of ctx.series) {
      if (o[sid] === undefined) continue;
      cellsSeen += 1;
      if (!(isObject(o[sid]) && "v" in o[sid])) {
        cellsWithoutV += 1;
        oddKeys = oddKeys || (isObject(o[sid]) ? Object.keys(o[sid]).join(", ") : typeof o[sid]);
      }
    }
  }
  if (cellsSeen && cellsWithoutV / cellsSeen > 0.5) {
    return dataset("schema_drift", `the value key v is missing in ${cellsWithoutV} of ${cellsSeen} cells; found: ${oddKeys}`), out;
  }
  out.stats.unknown_series = [...new Set(dated.flatMap((o) => Object.keys(o)))].filter((k) => k !== "d" && !ctx.series.includes(k));
  const absent = new Set(ctx.series.filter((sid) => !dated.some((o) => sid in o)));
  for (const sid of absent) dataset("missing_series", `${sid} appears in none of ${obs.length} rows`, `series:${sid}`);
  const present = ctx.series.filter((sid) => !absent.has(sid));

  const todayMs = parseDay(ctx.today);
  const seen = new Map();
  const days = [];
  for (const o of dated) {
    const ms = parseDay(o.d);
    const label = String(o.d).slice(0, 20);
    const problem =
      ms === null ? "is not a calendar date (expected YYYY-MM-DD)"
      : [0, 6].includes(new Date(ms).getUTCDay()) ? "falls on a weekend"
      : ms > todayMs + ctx.futureToleranceDays * DAY_MS ? "is in the future"
      : null;
    if (problem) {
      out.row_failures.push(failure("bad_date", `date ${label} ${problem}`, { scope: `date:${label}`, payload: { obs_date: label } }));
      out.stats.cells += present.length;
      out.stats.invalid += present.length;
      continue;
    }
    days.push(ms);
    for (const sid of present) {
      out.stats.cells += 1;
      const scope = `${sid}@${o.d}`;
      const bad = (type, detail, rawValue) => {
        out.row_failures.push(failure(type, detail, { scope, payload: { series: sid, obs_date: o.d, raw: rawValue } }));
        out.stats.invalid += 1;
      };
      if (o[sid] === undefined) { bad("null_value", "the cell is absent in this row", null); continue; }
      const raw = isObject(o[sid]) ? o[sid].v : o[sid];
      if (raw === null || raw === undefined || (typeof raw === "string" && raw.trim() === "")) { bad("null_value", "v is null or empty", raw ?? null); continue; }
      let value = NaN;
      if (typeof raw === "number") value = raw;
      else if (typeof raw === "string" && /^\d+(\.\d+)?$/.test(raw.trim())) value = Number(raw.trim());
      if (!Number.isFinite(value)) { bad("bad_type", `v is ${JSON.stringify(raw).slice(0, 40)}, not a plain decimal number`, raw); continue; }
      const [lo, hi] = ctx.ranges[sid] || [0, Infinity];
      if (!(value > 0 && value >= lo && value <= hi)) { bad("out_of_range", `${value} is outside ${lo}..${hi}`, raw); continue; }
      const prior = seen.get(scope);
      if (!prior) { seen.set(scope, { value, conflict: false, obs_date: o.d, series: sid }); continue; }
      if (Math.abs(prior.value - value) < 1e-12) out.stats.duplicates_dropped += 1;
      else if (!prior.conflict) { prior.conflict = true; bad("duplicate_conflict", `${scope} is ${prior.value} and ${value}`, raw); }
    }
  }
  out.valid_rows = [...seen.values()]
    .filter((r) => !r.conflict)
    .map((r) => ({ obs_date: r.obs_date, series: r.series, rate: r.value }))
    .sort((a, b) => (a.obs_date + a.series < b.obs_date + b.series ? -1 : 1));
  out.stats.valid = out.valid_rows.length;
  if (days.length) {
    const newest = Math.max(...days);
    const age = Math.floor((todayMs - newest) / DAY_MS);
    if (age > ctx.maxAgeDays) dataset("stale_data", `newest observation is ${new Date(newest).toISOString().slice(0, 10)}, ${age} days old (limit ${ctx.maxAgeDays})`);
  }
  return out;
}

const sameProblem = (claimed, x) => claimed.scope === x.scope && (claimed.kind === "row" || claimed.error_type === x.error_type);

// Which of the failures a retry claimed are fixed, judged on a fresh fetch (fetchFailure) and its validation (v).
function resolveClaimed(claimed, v, fetchFailure, batchOutcome) {
  const valid = new Set(v.valid_rows.map((r) => `${r.series}@${r.obs_date}`));
  const bad = new Map(v.row_failures.map((f) => [f.scope, f]));
  const fatal = v.dataset_failures.find((d) => FATAL.has(d.error_type));
  return claimed.map((f) => {
    const no = (x) => ({ failure_id: f.failure_id, ok: false, error_type: x.error_type, detail: x.detail, http_status: x.http_status, retry_after_s: x.retry_after_s });
    const yes = (detail) => ({ failure_id: f.failure_id, ok: true, detail });
    if (fetchFailure) return no(fetchFailure);
    if (f.kind === "fetch") return yes("the source answered");
    if (f.kind === "dataset") {
      const hit = v.dataset_failures.find((d) => d.error_type === f.error_type && d.scope === f.scope) || fatal;
      return hit ? no(hit) : yes("the dataset is healthy again");
    }
    if (f.kind === "audit") {
      return batchOutcome === "held" ? no({ error_type: "audit_jump", detail: "the batch was held again" }) : yes("the audit passed");
    }
    if (f.kind === "row") {
      if (valid.has(f.scope)) return yes("the row is valid now");
      if (fatal) return no(fatal);
      return no(bad.get(f.scope) || { error_type: f.error_type, detail: "the row is not in the latest payload" });
    }
    return no({ error_type: f.error_type, detail: `kind ${f.kind} is not retried automatically` });
  });
}

// Problems this fetch found that a claimed failure is not already tracking.
function newFailures(v, fetchFailure, claimed) {
  const all = fetchFailure ? [{ ...fetchFailure, scope: "source" }] : [...v.dataset_failures, ...v.row_failures];
  return all.filter((x) => !claimed.some((c) => sameProblem(c, x)));
}

// Oldest date any claimed row failure is about, so the retry fetch reaches back far enough to see it again.
function planWindow(claimed) {
  const days = claimed
    .filter((f) => f.kind === "row")
    .map((f) => f.scope.split("@")[1] || "")
    .filter((d) => parseDay(d) !== null)
    .sort();
  return { start_date: days.length ? days[0] : null };
}

function emptyValidation() {
  const stats = { observations: 0, cells: 0, valid: 0, invalid: 0, duplicates_dropped: 0, unknown_series: [] };
  return { fatal: false, dataset_failures: [], row_failures: [], valid_rows: [], stats };
}

// failed beats held beats partial beats success
function runStatus(fetchFailure, fatal, failureCount, batchOutcome) {
  if (fetchFailure || fatal) return "failed";
  if (batchOutcome === "held") return "held";
  return failureCount > 0 ? "partial" : "success";
}

// ---- alerts: pure functions so the wording and the grouping are tested like everything else ----

function deadAlerts(items, runbookUrl) {
  const byType = new Map();
  for (const it of items) byType.set(it.error_type, [...(byType.get(it.error_type) || []), it]);
  return [...byType].map(([type, list]) => {
    const severity = list.some((i) => i.severity === "critical") ? "critical" : "warning";
    const lines = list.slice(0, 10).map((i) => `- ${i.scope}: ${i.detail} (attempts: ${i.attempts})`);
    const more = list.length > 10 ? [`... and ${list.length - 10} more`] : [];
    return {
      key: `dead:${type}`,
      severity,
      subject: `[lab] ${list.length} failure(s) need a person: ${type}`,
      body: [`${list.length} failure(s) of type ${type} are dead-lettered.`, "", ...lines, ...more, "",
        `Runbook: ${runbookUrl}#${list[0].runbook}`, "After the fix: POST /v1/failures/<failure_id>/requeue"].join("\n"),
    };
  });
}

function heldAlert(batch, runbookUrl) {
  const worst = (batch.audit.worst || []).map((w) => `- ${w.series} on ${w.obs_date}: ${w.move_pct}%`);
  return {
    key: "batch_held",
    severity: "critical",
    subject: "[lab] a batch was held by the audit and is not published",
    body: [`Batch ${batch.batch_id}: ${batch.audit.summary}.`, "", ...worst, "",
      "Check the numbers against the source. If they are right: POST /v1/batches/<batch_id>/approve.",
      `Runbook: ${runbookUrl}#audit-jump`].join("\n"),
  };
}

// Rules the monitor workflow applies to /v1/health/summary. Silence is a failure too: no success for 45 minutes alerts.
function evaluateHealth(h, runbookUrl, limits = {}) {
  const lim = { maxMinutesSinceSuccess: 45, maxRunFailureRate: 0.5, minRunsForRate: 3, maxCellFailureRate: 0.1, maxOldestOpenMinutes: 120, ...limits };
  const alerts = [];
  const add = (key, severity, subject, body, anchor) => alerts.push({ key, severity, subject, body: `${body}
Runbook: ${runbookUrl}#${anchor}` });
  const stale = h.minutes_since_success === null ? h.runs_1h > 0 || h.queue.pending + h.queue.dead > 0 : h.minutes_since_success > lim.maxMinutesSinceSuccess;
  if (stale) {
    const since = h.minutes_since_success === null ? "ever" : `for ${h.minutes_since_success} minutes`;
    add("no_success", "critical", `[lab] no successful ingest ${since}`, `Last success: ${h.last_success_at || "never"}. Runs in the last hour: ${h.runs_1h}.`, "stale-data");
  }
  if (h.runs_1h >= lim.minRunsForRate && h.run_failure_rate_1h >= lim.maxRunFailureRate) {
    add("run_failure_rate", "warning", `[lab] ${Math.round(h.run_failure_rate_1h * 100)}% of ingest runs failed in the last hour`, `${h.bad_runs_1h} of ${h.runs_1h} runs failed or were held.`, "http-5xx");
  }
  if (h.cell_failure_rate_1h !== null && h.cell_failure_rate_1h >= lim.maxCellFailureRate) {
    add("cell_failure_rate", "warning", `[lab] ${Math.round(h.cell_failure_rate_1h * 100)}% of cells failed validation in the last hour`, "Look at which error types dominate (sql/rca/01_failure_rate_by_type.sql).", "null-value");
  }
  if (h.oldest_open_minutes !== null && h.oldest_open_minutes > lim.maxOldestOpenMinutes) {
    add("stuck_failure", "warning", `[lab] the oldest open failure is ${Math.round(h.oldest_open_minutes)} minutes old`, `${h.queue.pending} pending, ${h.queue.in_progress} in progress, ${h.queue.dead} dead.`, "workflow-error");
  }
  return alerts;
}

// What the error workflow gets from n8n's Error Trigger.
function errorAlert(err, runbookUrl) {
  const wf = (err.workflow && err.workflow.name) || "unknown workflow";
  const node = (err.execution && err.execution.lastNodeExecuted) || "unknown node";
  const message = (err.execution && err.execution.error && err.execution.error.message) || "no message";
  return {
    key: `workflow_error:${wf}`,
    severity: "critical",
    subject: `[lab] workflow failed: ${wf}`,
    body: [`${wf} stopped at node "${node}".`, `Error: ${message}`, `Execution: ${(err.execution && err.execution.url) || "n/a"}`, `Runbook: ${runbookUrl}#workflow-error`].join("\n"),
    wf,
    message,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    RANGES, FATAL, ctxWithDefaults, parseDay, parseRetryAfter, classifyFetch, parseBody, validatePayload,
    resolveClaimed, newFailures, planWindow, emptyValidation, runStatus, deadAlerts, heldAlert, evaluateHealth, errorAlert,
  };
}
