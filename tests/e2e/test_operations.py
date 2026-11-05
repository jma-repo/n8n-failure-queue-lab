import threading
import time

import pytest
from helpers import mails_with

from lab.stack import wait_until

pytestmark = pytest.mark.e2e


def values(lab):
    return {(r["obs_date"], r["series"]): r["rate"] for r in lab.rates()}


def test_a_jump_that_passes_every_row_check_is_held_by_the_audit_until_a_person_approves_it(lab):
    lab.run_ingest()
    before = values(lab)
    assert len(before) == 40
    lab.scenario("jump", pct=0.10)
    assert lab.run_ingest()["status"] == "held"
    assert values(lab) == before
    held = next(b for b in lab.batches() if b["status"] == "held")
    assert any(f["error_type"] == "audit_jump" and f["status"] == "pending" for f in lab.failures())
    wait_until(lambda: mails_with(lab, "held by the audit") >= 1, timeout=20, what="the held-batch email")
    lab.api.post(f"/v1/batches/{held['batch_id']}/approve").raise_for_status()
    after = values(lab)
    assert len(after) == 40 and after != before


def test_the_monitor_alerts_on_silence_and_mutes_repeats_during_the_cooldown(lab):
    lab.reset(time_scale=0.01, jitter="none")  # the critical cool-down becomes 9 seconds
    lab.scenario("http_500")
    lab.run_ingest()
    lab.trigger("monitor")
    wait_until(lambda: mails_with(lab, "no successful ingest") == 1, timeout=20, what="the silence alert")
    lab.trigger("monitor")
    time.sleep(3)
    assert mails_with(lab, "no successful ingest") == 1
    time.sleep(7)
    lab.trigger("monitor")
    wait_until(lambda: mails_with(lab, "no successful ingest") == 2, timeout=20, what="the second alert after the cool-down")
    assert [a["sent"] for a in lab.alerts() if a["alert_key"] == "no_success"].count(False) >= 1


def test_when_the_queue_api_is_down_the_error_workflow_still_gets_the_alert_out(lab):
    lab.api.post("/admin/outage", json={"seconds": 60}).raise_for_status()
    lab.trigger("ingest")
    wait_until(lambda: mails_with(lab, "workflow failed: Lab: ingest FX rates") >= 1, timeout=60, what="the error-handler email")
    lab.api.delete("/admin/outage").raise_for_status()
    assert lab.run_ingest()["status"] == "success"


def test_two_retry_workers_at_once_do_not_work_the_same_failure_twice(lab):
    lab.scenario("http_500")
    lab.run_ingest()
    time.sleep(1)  # past the first retry delay
    workers = [threading.Thread(target=lab.trigger, args=("retry",)) for _ in range(2)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    time.sleep(6)
    (item,) = lab.failures()
    assert item["attempts"] == 1


def test_a_failure_that_needs_a_person_can_be_requeued_after_the_fix(lab):
    lab.scenario("schema_drift")
    lab.run_ingest()
    (item,) = lab.failures("dead")
    lab.sim.delete("/admin/scenario")
    lab.api.post(f"/v1/failures/{item['failure_id']}/requeue").raise_for_status()
    wait_until(lambda: lab.failures("pending"), timeout=10, what="the requeued failure")
    from helpers import drive_retries

    drive_retries(lab, lambda: lab.failures("resolved"))
    assert len(lab.rates()) == 40
