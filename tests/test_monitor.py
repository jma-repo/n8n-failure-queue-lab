from datetime import timedelta

from lab.queue_api.store import Policy, Store


def run(run_id, started, status, rows_in=40, rows_failed=0, **extra):
    return {
        "run_id": run_id, "workflow": "ingest", "trigger_kind": "schedule", "started_at": started,
        "finished_at": started + timedelta(seconds=5), "status": status, "rows_in": rows_in,
        "rows_ok": rows_in - rows_failed, "rows_failed": rows_failed, **extra,
    }  # fmt: skip


def test_the_same_alert_is_muted_during_its_cooldown_and_sent_again_after(store, clock):
    assert store.post_alert("dead:http_5xx", "critical", "3 dead")["send"] is True
    clock.advance(minutes=10)
    muted = store.post_alert("dead:http_5xx", "critical", "3 dead")
    assert muted["send"] is False and "cool-down 900s" in muted["suppressed_reason"]
    clock.advance(minutes=6)
    assert store.post_alert("dead:http_5xx", "critical", "3 dead")["send"] is True


def test_a_different_alert_key_is_not_muted_by_another(store):
    store.post_alert("dead:http_5xx", "critical", "a")
    assert store.post_alert("dead:schema_drift", "critical", "b")["send"] is True


def test_warnings_stay_muted_longer_than_critical_alerts(store, clock):
    store.post_alert("k", "warning", "w")
    clock.advance(minutes=30)
    assert store.post_alert("k", "warning", "w")["send"] is False
    clock.advance(minutes=31)
    assert store.post_alert("k", "warning", "w")["send"] is True


def test_the_cooldown_follows_time_scale(clock):
    fast = Store(":memory:", policy=Policy(time_scale=0.01), clock=clock)
    fast.post_alert("k", "critical", "x")
    clock.advance(seconds=5)
    assert fast.post_alert("k", "critical", "x")["send"] is False
    clock.advance(seconds=5)
    assert fast.post_alert("k", "critical", "x")["send"] is True


def test_muted_alerts_are_still_logged(store, clock):
    store.post_alert("k", "critical", "x")
    store.post_alert("k", "critical", "x")
    rows = store.list_alerts()
    assert [r["sent"] for r in rows] == [False, True]
    assert rows[0]["suppressed_reason"]


def test_recording_a_run_twice_updates_it(store, clock):
    store.record_run(run("r1", clock.now, "failed", rows_in=0))
    store.record_run(run("r1", clock.now, "success", rows_in=40, note="after retry"))
    (row,) = store.list_runs()
    assert (row["status"], row["rows_in"], row["note"]) == ("success", 40, "after retry")


def test_health_with_no_runs_says_so(store):
    h = store.health()
    assert h["last_success_at"] is None and h["minutes_since_success"] is None
    assert h["run_failure_rate_1h"] is None and h["queue"]["dead"] == 0


def test_health_reports_freshness_failure_rates_and_the_backlog(store, clock):
    now = clock.now
    store.record_run(run("r1", now - timedelta(minutes=30), "success"))
    store.record_run(run("r2", now - timedelta(minutes=20), "failed", rows_in=0))
    store.record_run(run("r3", now - timedelta(minutes=10), "partial", rows_failed=4))
    store.enqueue("r2", [{"error_type": "http_5xx", "scope": "source"}, {"error_type": "schema_drift", "scope": "dataset"}])
    clock.advance(minutes=2)
    h = store.health()
    assert h["minutes_since_success"] == 11.9
    assert (h["runs_1h"], h["bad_runs_1h"]) == (3, 1)
    assert h["run_failure_rate_1h"] == 0.3333
    assert h["cell_failure_rate_1h"] == 0.05
    assert h["queue"] == {"pending": 1, "in_progress": 0, "dead": 1, "resolved": 0}
    assert h["due_now"] == 1 and h["oldest_open_minutes"] == 2.0
