"""Replays two weeks of operations through the real store: ingest every 15 minutes, retry worker every 5, alerts and all.

Only what the world does is scripted (which request meets which fault, when a person acts). Queueing, backoff, dead-lettering,
audits and alert cool-downs are the store's own code, so the ledger the RCA queries read has the same shape as the live one.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from lab.history.incidents import DAYS, HUMAN_ACTIONS, START, fault_at
from lab.queue_api.store import Policy, Store
from lab.source_sim import data

FETCH_FAULTS = {"http_5xx": 500, "timeout": None, "rate_limited": 429}
SERIES = list(data.SERIES)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def rows_for(t: datetime, jump: bool) -> list[dict[str, Any]]:
    """Five business days of rates as the source shows them at t; with jump, the newest day restated 8% higher."""
    days = data.business_days(data.latest_published(t), 5)
    rows = []
    for d in days:
        for sid in SERIES:
            rate = data.rate(sid, d) * (1.08 if jump and d == days[-1] else 1.0)
            rows.append({"obs_date": d, "series": sid, "rate": round(rate, 4)})
    return rows


def dead_alerts(store: Store, dead: list[dict[str, Any]], run_id: str, now: datetime) -> int:
    """The same grouping n8n does: one alert per error type, muted by the store's cool-down."""
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in dead:
        by_type[item["error_type"]].append(item)
    sent = 0
    for error_type, items in by_type.items():
        severity = "critical" if any(i["severity"] == "critical" for i in items) else "warning"
        subject = f"[lab] {len(items)} failure(s) need a person: {error_type}"
        sent += store.post_alert(f"dead:{error_type}", severity, subject, run_id=run_id, now=now)["send"]
    return sent


class Simulation:
    def __init__(self, store: Store, clock: Clock, rng: random.Random) -> None:
        self.store, self.clock, self.rng = store, clock, rng
        self.published_day = None
        self.counters: dict[str, int] = defaultdict(int)

    def ingest(self, t: datetime) -> None:
        run_id = f"ingest-{t:%Y%m%d%H%M}"
        fault = fault_at(t, self.rng)
        latest = data.latest_published(t)
        status, source_status, rows_in, rows_failed, failures = "success", 200, 40, 0, []
        if fault in FETCH_FAULTS:
            status, source_status, rows_in = "failed", FETCH_FAULTS[fault], 0
            retry_after = 600 if fault == "rate_limited" else None
            failures = [{"error_type": fault, "scope": "source", "detail": f"simulated {fault}", "http_status": source_status, "retry_after_s": retry_after}]  # fmt: skip
        elif fault == "schema_drift":
            status, rows_in = "failed", 0
            failures = [{"error_type": "schema_drift", "scope": "dataset", "detail": "the value key v is missing in 40 of 40 cells"}]
        elif fault == "null_value":
            status, rows_failed = "partial", 1
            failures = [{"error_type": "null_value", "scope": f"FXCNYCAD@{latest}", "detail": "v is null or empty"}]
        queued = self.store.enqueue(run_id, failures, now=t) if failures else {"dead_on_arrival": []}
        self.counters["alerts_sent"] += dead_alerts(self.store, queued["dead_on_arrival"], run_id, t)
        publishing = fault not in FETCH_FAULTS and fault != "schema_drift"
        if publishing and (latest != self.published_day or fault == "audit_jump"):
            out = self.store.stage_and_publish(run_id, "ingest", rows_for(t, fault == "audit_jump"), now=t)
            if out["outcome"] == "published":
                self.published_day = latest
            if out["outcome"] == "held":
                status = "held"
                alert = self.store.post_alert("batch_held", "critical", "[lab] a batch was held by the audit", run_id=run_id, now=t)
                self.counters["alerts_sent"] += alert["send"]
        elif fault == "audit_jump":
            status = "held"  # the same restated rates were already held by an earlier run
        self.store.record_run({
            "run_id": run_id, "workflow": "ingest", "trigger_kind": "schedule", "started_at": t,
            "finished_at": t + timedelta(seconds=2), "status": status, "source_status": source_status,
            "rows_in": rows_in, "rows_ok": rows_in - rows_failed, "rows_failed": rows_failed,
            "duration_ms": 1800 if fault == "timeout" else 400,
        })  # fmt: skip
        self.counters["ingest_runs"] += 1

    def judge(self, item: dict[str, Any], fault: str | None) -> dict[str, Any]:
        """Would this claimed failure be fixed by a fetch made now? Mirrors resolveClaimed in n8n/code/validate.js."""
        fid, kind = item["failure_id"], item["kind"]
        bad = {"failure_id": fid, "ok": False, "error_type": fault, "detail": f"still {fault}"}
        if fault in FETCH_FAULTS:
            return {**bad, "http_status": FETCH_FAULTS[fault], "retry_after_s": 600 if fault == "rate_limited" else None}
        still_broken = (
            (kind == "dataset" and fault == "schema_drift")
            or (kind == "row" and fault in ("null_value", "schema_drift"))
            or (kind == "audit" and fault == "audit_jump")
        )
        return bad if still_broken else {"failure_id": fid, "ok": True}

    def retry(self, t: datetime) -> None:
        claimed = self.store.claim(limit=50, now=t)
        if not claimed:
            return
        run_id = f"retry-{t:%Y%m%d%H%M}"
        fault = fault_at(t, self.rng)
        out = self.store.apply_results(run_id, [self.judge(c, fault) for c in claimed], now=t)
        self.counters["alerts_sent"] += dead_alerts(self.store, out["dead"], run_id, t)
        note = f"claimed {len(claimed)}: resolved {len(out['resolved'])}, rescheduled {len(out['rescheduled'])}, dead {len(out['dead'])}"
        self.store.record_run({
            "run_id": run_id, "workflow": "retry", "trigger_kind": "schedule", "started_at": t,
            "finished_at": t + timedelta(seconds=2), "status": "failed" if fault in FETCH_FAULTS else "success",
            "source_status": FETCH_FAULTS.get(fault, 200), "rows_in": 0, "rows_ok": 0, "rows_failed": 0,
            "duration_ms": 500, "note": note,
        })  # fmt: skip
        self.counters["retry_runs"] += 1

    def human(self, t: datetime) -> None:
        """A person requeues what retrying gave up on, after the cause was fixed."""
        for when, action in HUMAN_ACTIONS:
            if when == t and action == "requeue_dead":
                for item in self.store.list_failures("dead"):
                    self.store.requeue(item["failure_id"], now=t)
                    self.counters["requeued"] += 1


def simulate(path: str = ":memory:", seed: int = 7, days: int = DAYS) -> tuple[Store, dict[str, int]]:
    """Build the history in a DuckDB file (or in memory) and return the store and a few counters."""
    clock = Clock(START)
    store = Store(path, policy=Policy(jitter="equal"), clock=clock, rng=random.Random(seed))
    store.reset()
    sim = Simulation(store, clock, random.Random(seed + 1))
    t = START
    while t < START + timedelta(days=days):
        clock.now = t
        sim.human(t)
        if t.minute % 15 == 0:
            sim.ingest(t)
        sim.retry(t)
        t += timedelta(minutes=5)
    return store, dict(sim.counters)
