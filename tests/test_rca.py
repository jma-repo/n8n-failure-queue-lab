"""The blind test: the RCA queries never see the scripted incidents, yet they have to find them."""

from datetime import datetime

import pytest

from lab import rca
from lab.history.incidents import INCIDENTS, START
from lab.history.simulate import simulate


@pytest.fixture(scope="module")
def results():
    store, counters = simulate()
    assert counters["ingest_runs"] == 1344
    return {r.name.split("_")[0]: r for r in rca.run_all(store.con)}


def rows(result):
    return [dict(zip(result.columns, row, strict=True)) for row in result.rows]


def test_every_query_documents_itself():
    for path in rca.query_files():
        head = rca.parse_header(path.read_text(encoding="utf-8"))
        assert set(head) == set(rca.HEADER_KEYS), path.name


def test_the_same_seed_gives_the_same_history():
    first, second = simulate(days=3)[0], simulate(days=3)[0]
    query = "SELECT run_id, status, rows_in FROM runs ORDER BY run_id"
    assert first._all(query) == second._all(query)
    assert first._all("SELECT count(*) AS n FROM failure_events") == second._all("SELECT count(*) AS n FROM failure_events")


def test_the_nightly_maintenance_window_is_the_only_recurring_hour(results):
    flagged = [r for r in rows(results["02"]) if r["flag"]]
    assert [(r["hour_utc"], r["flag"], r["main_error"]) for r in flagged] == [(7, "RECURRING", "timeout")]


def test_the_spike_days_are_the_outage_the_drift_and_the_null_day(results):
    spikes = [r["day"].isoformat() for r in rows(results["03"]) if r["flag"] == "SPIKE"]
    assert spikes == ["2026-10-14", "2026-10-17", "2026-10-18", "2026-10-20"]


def find(incidents, key):
    return next(r for r in incidents if r["main_error"] == key and r["bad_runs"] >= 3)


def test_the_long_incidents_are_found_with_their_start_end_and_cause(results):
    found = rows(results["04"])
    for key, error in (("upstream-outage", "http_5xx"), ("schema-drift", "schema_drift"), ("null-cny", "null_value")):
        truth = next(i for i in INCIDENTS if i.key == key)
        hit = find(found, error)
        assert abs((hit["started_at"] - truth.start).total_seconds()) <= 15 * 60, key
        assert abs((hit["recovered_at"] - truth.end).total_seconds()) <= 15 * 60, key
    assert find(found, "audit_jump")["minutes_to_alert"] == 0


def test_an_outage_is_only_announced_when_retries_run_out_but_a_drift_is_announced_at_once(results):
    found = rows(results["04"])
    assert 55 <= find(found, "http_5xx")["minutes_to_alert"] <= 75
    assert find(found, "schema_drift")["minutes_to_alert"] == 0


def test_error_patterns_separate_the_chronic_from_the_episodic(results):
    patterns = {r["error_type"]: r["pattern"] for r in rows(results["05"])}
    assert patterns == {"timeout": "chronic", "http_5xx": "episodic", "schema_drift": "episodic",
                        "null_value": "one-off", "rate_limited": "one-off", "audit_jump": "one-off"}  # fmt: skip
    first = {r["error_type"]: r["first_day"] for r in rows(results["05"])}
    assert first["schema_drift"] == (START.replace(day=17)).date()


def test_retries_heal_the_transients_and_a_person_closes_what_cannot_heal(results):
    by_type = {r["error_type"]: r for r in rows(results["06"])}
    assert by_type["timeout"]["self_healed_pct"] == 100
    assert by_type["schema_drift"]["healed_after_a_person"] == 1 and by_type["schema_drift"]["retry_budget"] == 1
    assert by_type["null_value"]["dead_now"] == 1
    assert by_type["http_5xx"]["healed_after_a_person"] == 1
    assert by_type["ALL"]["failures"] == sum(r["failures"] for k, r in by_type.items() if k != "ALL")


def test_the_backoff_policy_is_followed(results):
    total = next(r for r in rows(results["07"]) if r["error_type"] == "ALL")
    assert total["fired_early"] == 0 and total["outside_envelope"] == 0
    assert total["late_p95_s"] <= 300 and total["verdict"] == "PASS"


def test_the_post_mortem_has_one_row_per_cause_and_says_who_has_to_act(results):
    table = rows(results["08"])
    outcomes = {r["main_error"]: r["outcome"] for r in table}
    assert outcomes["null_value"] == "dead-lettered, needs a person"
    assert outcomes["schema_drift"] == outcomes["http_5xx"] == "fixed by a person (requeued)"
    assert outcomes["timeout"] == "self-healed by retries"
    assert next(r for r in table if r["main_error"] == "timeout")["what"].endswith("(a daily pattern)")
    assert all(isinstance(r["first_started_at"], datetime) for r in table)
