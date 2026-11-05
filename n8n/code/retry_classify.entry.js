// uses: validate
// Retry: the same classification and validation as ingest, kept whole so each claimed failure can be judged on it.
const resp = $input.first().json;
const plan = $("Plan fetch").first().json;
const ctx = { series: plan.series, today: plan.started_at.slice(0, 10) };
const fetchFailure = classifyFetch(resp);
const v = fetchFailure ? emptyValidation() : validatePayload(resp.body ?? resp.data, ctx);
return [{
  json: { ...plan, source_status: resp.statusCode ?? null, fetch_failure: fetchFailure, fatal: v.fatal, stats: v.stats, rows: v.valid_rows, validation: v },
}];
