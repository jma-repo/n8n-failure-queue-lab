// uses: validate
// Ingest: classify the HTTP result, validate the payload cell by cell, and list what to queue and what to publish.
const resp = $input.first().json;
const run = $("Start run").first().json;
const ctx = { series: run.series, today: run.started_at.slice(0, 10) };
const fetchFailure = classifyFetch(resp);
const v = fetchFailure ? emptyValidation() : validatePayload(resp.body ?? resp.data, ctx);
const failures = fetchFailure ? [{ ...fetchFailure, scope: "source" }] : [...v.dataset_failures, ...v.row_failures];
return [{
  json: {
    ...run,
    source_status: resp.statusCode ?? null,
    fetch_failure: fetchFailure,
    fatal: v.fatal,
    stats: v.stats,
    failures,
    rows: v.valid_rows,
  },
}];
