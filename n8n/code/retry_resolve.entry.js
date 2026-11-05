// uses: validate
// Retry: which claimed failures are fixed, and which problems in this fetch nobody is tracking yet.
const c = $("Classify and validate").first().json;
const p = $("Publish rows").first().json;
return [{
  json: {
    run_id: c.run_id,
    results: resolveClaimed(c.claimed, c.validation, c.fetch_failure, p.outcome),
    new_failures: newFailures(c.validation, c.fetch_failure, c.claimed),
  },
}];
