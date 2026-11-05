// uses: none
// One item per alert; no alerts means nothing downstream runs.
const alerts = $("__SOURCE__").first().json.alerts || [];
return alerts.map((a) => ({ json: a }));
