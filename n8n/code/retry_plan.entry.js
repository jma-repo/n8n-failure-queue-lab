// uses: validate
// Retry: reach back far enough to see the oldest cell a claimed failure is about.
const claimed = $input.first().json.items;
const run = $("Start run").first().json;
const { start_date } = planWindow(claimed);
const base = run.source_url.replace(/\?.*$/, "");
return [{ json: { ...run, claimed, source_url: start_date ? `${base}?start_date=${start_date}` : run.source_url } }];
