import time
from datetime import datetime

import pytest
from helpers import drive_retries, mails_with

from lab.stack import wait_until

pytestmark = pytest.mark.e2e


def values(lab):
    return {(r["obs_date"], r["series"]): r["rate"] for r in lab.rates()}


def seconds_between(a: str, b: str) -> float:
    return (datetime.fromisoformat(b.rstrip("Z")) - datetime.fromisoformat(a.rstrip("Z"))).total_seconds()


def test_a_healthy_run_publishes_every_cell_and_a_repeat_changes_nothing(lab):
    first = lab.run_ingest()
    assert (first["status"], first["rows_in"], first["rows_ok"], first["rows_failed"]) == ("success", 40, 40, 0)
    assert first["n8n_execution_id"]
    assert len(lab.rates()) == 40
    second = lab.run_ingest()
    assert second["status"] == "success" and "unchanged 40" in second["note"]
    assert len(lab.batches()) == 1
    assert lab.failures() == [] and lab.alerts() == []


def test_a_source_that_recovers_is_caught_by_the_retry_worker(lab):
    lab.scenario("http_500", first_n=2)
    run = lab.run_ingest()
    assert (run["status"], run["source_status"]) == ("failed", 500)
    (item,) = lab.failures()
    assert (item["error_type"], item["status"]) == ("http_5xx", "pending")
    drive_retries(lab, lambda: lab.failures("resolved"))
    (item,) = lab.failures()
    assert item["attempts"] == 2
    events = [e["event"] for e in lab.api.get(f"/v1/failures/{item['failure_id']}/events").json()["items"]]
    assert events == ["detected", "retry_failed", "retry_ok"]
    assert len(lab.rates()) == 40
    assert lab.alerts() == []


def test_a_source_that_stays_down_backs_off_exponentially_then_dead_letters_and_alerts_once(lab):
    lab.scenario("http_500")
    lab.run_ingest()
    (item,) = lab.failures()
    drive_retries(lab, lambda: lab.failures("dead"), timeout=90)
    events = lab.api.get(f"/v1/failures/{item['failure_id']}/events").json()["items"]
    delays = [e["retry_in_s"] for e in events if e["retry_in_s"]]
    assert delays == pytest.approx([0.3, 0.6, 1.2, 2.4, 4.8, 9.6])
    assert [e["event"] for e in events][-2:] == ["retry_failed", "dead_lettered"]
    wait_until(lambda: mails_with(lab, "need a person: http_5xx") >= 1, timeout=20, what="the dead-letter email")
    time.sleep(2)
    assert mails_with(lab, "need a person: http_5xx") == 1


def test_schema_drift_goes_straight_to_a_person(lab):
    lab.scenario("schema_drift")
    assert lab.run_ingest()["status"] == "failed"
    (item,) = lab.failures()
    assert (item["error_type"], item["status"], item["attempts"]) == ("schema_drift", "dead", 0)
    wait_until(lambda: mails_with(lab, "schema_drift") >= 1, timeout=20, what="the alert email")
    assert lab.rates() == []


@pytest.mark.parametrize(
    ("scenario", "params", "error_type", "scope_prefix"),
    [
        ("out_of_range", {"series": "FXJPYCAD"}, "out_of_range", "FXJPYCAD@"),
        ("null_values", {"series": "FXEURCAD"}, "null_value", "FXEURCAD@"),
        ("bad_type", {"series": "FXGBPCAD", "text": "1,80"}, "bad_type", "FXGBPCAD@"),
        ("duplicates", {}, "duplicate_conflict", "FXUSDCAD@"),
    ],
)
def test_bad_cells_are_queued_while_the_rest_is_published_and_the_retry_closes_them(lab, scenario, params, error_type, scope_prefix):
    lab.scenario(scenario, **params)
    run = lab.run_ingest()
    assert (run["status"], run["rows_failed"]) == ("partial", 1)
    (item,) = lab.failures()
    assert (item["error_type"], item["status"], item["kind"]) == (error_type, "pending", "row")
    assert item["scope"].startswith(scope_prefix)
    assert len(lab.rates()) == 39
    lab.sim.delete("/admin/scenario")
    drive_retries(lab, lambda: lab.failures("resolved"))
    assert len(lab.rates()) == 40


def test_a_stale_feed_still_loads_what_it_has_and_raises_a_dataset_failure(lab):
    lab.scenario("stale", days=7)
    assert lab.run_ingest()["status"] == "partial"
    (item,) = lab.failures()
    assert (item["error_type"], item["kind"], item["status"]) == ("stale_data", "dataset", "pending")
    assert len(lab.rates()) == 40


def test_a_429_waits_at_least_as_long_as_retry_after(lab):
    lab.scenario("rate_limit", retry_after_s=2000)  # 2000 s x 0.005 = 10 s, far above the 0.6 s backoff
    lab.run_ingest()
    (item,) = lab.failures()
    assert item["error_type"] == "rate_limited"
    assert 9 <= seconds_between(item["first_seen_at"], item["next_retry_at"]) <= 11


def test_a_source_that_hangs_is_timed_out_tried_three_times_in_the_flow_and_queued(lab):
    lab.scenario("timeout", hang_s=14)
    run = lab.run_ingest(timeout=120)
    assert run["status"] == "failed" and run["source_status"] is None
    (item,) = lab.failures()
    assert item["error_type"] == "timeout"
    assert lab.sim.get("/admin/scenario").json()["requests"] >= 3
