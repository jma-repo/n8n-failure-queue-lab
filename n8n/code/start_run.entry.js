// uses: none
// Names the run and works out where to fetch from. The build script fills in __WORKFLOW__.
const first = $input.first().json;
const fromWebhook = first.headers !== undefined; // webhook items carry request headers, schedule items do not
const body = fromWebhook ? first.body || {} : {};
const series = ($env.LAB_SERIES || "").split(",").filter(Boolean);
const startedMs = Date.now();
return [{
  json: {
    run_id: body.run_id || `__WORKFLOW__-${startedMs}-${Math.random().toString(36).slice(2, 6)}`,
    trigger_kind: fromWebhook ? "webhook" : "schedule",
    started_ms: startedMs,
    started_at: new Date(startedMs).toISOString(),
    series,
    source_url: `${$env.LAB_SOURCE_BASE}/valet/observations/${series.join(",")}/json?recent=5`,
  },
}];
