from datetime import timedelta

import pytest

from lab.queue_api.store import Policy, Store, UnknownErrorType


def fail(error_type="http_5xx", scope="source", **extra):
    return {"error_type": error_type, "scope": scope, "detail": "boom", **extra}


def test_a_failure_is_queued_with_its_first_retry_one_base_delay_away(store, clock):
    out = store.enqueue("run-1", [fail()])
    (row,) = store.list_failures()
    assert out["created"][0]["status"] == "pending"
    assert row["next_retry_at"] == clock.now + timedelta(seconds=60)
    assert (row["attempts"], row["max_attempts"], row["kind"]) == (0, 6, "fetch")
    events = store._all("SELECT event, attempt, retry_in_s FROM failure_events")
    assert events == [{"event": "detected", "attempt": 0, "retry_in_s": 60.0}]


def test_the_same_problem_seen_again_is_one_queue_item(store, clock):
    store.enqueue("run-1", [fail()])
    clock.advance(minutes=15)
    out = store.enqueue("run-2", [fail()])
    (row,) = store.list_failures()
    assert out["repeated"] == 1 and out["created"] == []
    assert row["occurrences"] == 2
    assert store._one("SELECT count(*) AS n FROM failure_events WHERE event = 'detected'")["n"] == 2


def test_a_resolved_problem_that_comes_back_is_queued_again(store, clock):
    first = store.enqueue("run-1", [fail()])["created"][0]["failure_id"]
    clock.advance(seconds=61)
    store.claim()
    store.apply_results("run-2", [{"failure_id": first, "ok": True}])
    again = store.enqueue("run-3", [fail()])
    assert len(again["created"]) == 1
    assert len(store.list_failures()) == 2


def test_a_failure_that_retrying_cannot_fix_is_dead_on_arrival(store):
    out = store.enqueue("run-1", [fail("schema_drift", "dataset")])
    (dead,) = out["dead_on_arrival"]
    assert dead["severity"] == "critical" and dead["runbook"] == "schema-drift"
    assert store.list_failures()[0]["status"] == "dead"
    names = [e["event"] for e in store._all("SELECT event FROM failure_events ORDER BY event_id")]
    assert names == ["detected", "dead_lettered"]


def test_unknown_error_types_are_refused_before_anything_is_written(store):
    with pytest.raises(UnknownErrorType):
        store.enqueue("run-1", [fail(), fail("made_up")])
    assert store.list_failures() == []


def test_retry_after_pushes_the_first_retry_out(store, clock):
    store.enqueue("run-1", [fail("rate_limited", retry_after_s=900)])
    assert store.list_failures()[0]["next_retry_at"] == clock.now + timedelta(seconds=900)


def test_time_scale_shrinks_every_delay(clock):
    fast = Store(":memory:", policy=Policy(jitter="none", time_scale=0.01), clock=clock)
    fast.enqueue("run-1", [fail()])
    assert fast.list_failures()[0]["next_retry_at"] == clock.now + timedelta(seconds=0.6)


def test_claim_returns_only_what_is_due_and_locks_it(store, clock):
    store.enqueue("run-1", [fail(), fail("timeout")])
    assert store.claim() == []
    clock.advance(seconds=60)
    claimed = store.claim()
    assert sorted(c["error_type"] for c in claimed) == ["http_5xx", "timeout"]
    assert store.claim() == []
    assert {r["status"] for r in store.list_failures()} == {"in_progress"}


def test_claim_respects_the_limit_and_takes_the_oldest_due_first(store, clock):
    for n in range(5):
        store.enqueue("run-1", [fail("null_value", f"S{n}@2026-11-04")])
        clock.advance(seconds=1)
    clock.advance(minutes=10)
    first = store.claim(limit=2)
    assert [c["scope"] for c in first] == ["S0@2026-11-04", "S1@2026-11-04"]
    assert len(store.claim(limit=10)) == 3


def test_a_lease_that_ran_out_makes_the_failure_due_again(store, clock):
    store.enqueue("run-1", [fail()])
    clock.advance(seconds=60)
    assert len(store.claim(lease_s=120)) == 1
    clock.advance(seconds=60)
    assert store.claim() == []
    clock.advance(seconds=61)
    assert len(store.claim()) == 1


def test_each_failed_retry_waits_twice_as_long_then_the_failure_is_dead_lettered(store, clock):
    store.enqueue("run-1", [fail()])
    waits = []
    for _ in range(5):
        clock.advance(days=1)
        (item,) = store.claim()
        out = store.apply_results("run-retry", [{"failure_id": item["failure_id"], "ok": False, "detail": "503"}])
        waits.append((out["rescheduled"][0]["next_retry_at"] - clock.now).total_seconds())
    assert waits == [120, 240, 480, 960, 1920]
    clock.advance(days=1)
    (item,) = store.claim()
    out = store.apply_results("run-retry", [{"failure_id": item["failure_id"], "ok": False, "detail": "503"}])
    assert [d["attempts"] for d in out["dead"]] == [6]
    assert out["dead"][0]["runbook"] == "http-5xx"
    assert store.list_failures()[0]["status"] == "dead"
    assert store.claim() == []


def test_a_successful_retry_resolves_the_failure(store, clock):
    fid = store.enqueue("run-1", [fail()])["created"][0]["failure_id"]
    clock.advance(seconds=60)
    store.claim()
    out = store.apply_results("run-2", [{"failure_id": fid, "ok": True}])
    assert out["resolved"] == [fid]
    row = store.list_failures()[0]
    assert (row["status"], row["attempts"], row["next_retry_at"]) == ("resolved", 1, None)


def test_a_late_or_repeated_result_changes_nothing(store, clock):
    fid = store.enqueue("run-1", [fail()])["created"][0]["failure_id"]
    result = {"failure_id": fid, "ok": False}
    assert store.apply_results("r", [result])["ignored"]
    clock.advance(seconds=60)
    store.claim()
    store.apply_results("r", [result])
    assert store.apply_results("r", [result])["ignored"][0]["failure_id"] == fid
    assert store.list_failures()[0]["attempts"] == 1


def test_a_failure_that_changes_shape_keeps_its_identity_and_records_what_was_seen(store, clock):
    fid = store.enqueue("run-1", [fail("null_value", "USD@2026-11-04")])["created"][0]["failure_id"]
    clock.advance(minutes=6)
    store.claim()
    store.apply_results("r", [{"failure_id": fid, "ok": False, "error_type": "out_of_range", "detail": "0"}])
    assert store.list_failures()[0]["error_type"] == "null_value"
    seen = store._one("SELECT error_type FROM failure_events WHERE event = 'retry_failed'")
    assert seen["error_type"] == "out_of_range"


def test_requeue_gives_a_dead_failure_a_new_budget(store):
    fid = store.enqueue("run-1", [fail("schema_drift", "dataset")])["created"][0]["failure_id"]
    assert store.requeue(fid)["status"] == "pending"
    assert store.list_failures()[0]["attempts"] == 0
    with pytest.raises(ValueError):
        store.requeue(fid)
    with pytest.raises(LookupError):
        store.requeue("nope")
