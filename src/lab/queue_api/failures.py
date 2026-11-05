"""Putting failures on the queue, listing them, and putting dead ones back."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from lab.queue_api.monitor import MonitorStore

INSERT_FAILURE = (
    "INSERT INTO failure_queue (failure_id, dedupe_key, kind, error_type, scope, error_detail, payload, run_id, status,"
    " attempts, max_attempts, next_retry_at, first_seen_at, last_seen_at, dead_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)"
)


def decode_payload(row: dict[str, Any]) -> dict[str, Any]:
    row["payload"] = json.loads(row["payload"]) if row.get("payload") else {}
    return row


class FailureStore(MonitorStore):
    def enqueue(self, run_id: str, items: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
        """Queue failures. One error type plus one scope is one queue item, however many runs see it again."""
        now = now or self.clock()
        for item in items:  # refuse unknown types before anything is written
            self.error_type(item["error_type"])
        created, dead, repeated = [], [], 0
        with self.lock:
            for item in items:
                et = self.error_type(item["error_type"])
                scope, detail = item["scope"], item.get("detail") or ""
                key = f"{et.name}|{scope}"
                open_item = self._one(
                    "SELECT failure_id FROM failure_queue WHERE dedupe_key = ? AND status <> 'resolved' LIMIT 1", [key]
                )
                if open_item:
                    self._run(
                        "UPDATE failure_queue SET last_seen_at = ?, occurrences = occurrences + 1 WHERE failure_id = ?",
                        [now, open_item["failure_id"]],
                    )
                    self.event(now, open_item["failure_id"], run_id, "detected", et, 0,
                               http_status=item.get("http_status"), detail=detail)  # fmt: skip
                    repeated += 1
                    continue
                failure_id = uuid.uuid4().hex[:12]
                if et.retryable:
                    delay = self.delay_s(et, 0, item.get("retry_after_s"))
                    status, next_retry = "pending", now + timedelta(seconds=delay)
                else:
                    delay, status, next_retry = None, "dead", None
                self._run(
                    INSERT_FAILURE,
                    [failure_id, key, et.kind, et.name, scope, detail, json.dumps(item.get("payload") or {}), run_id,
                     status, et.max_attempts, next_retry, now, now, now if status == "dead" else None],
                )  # fmt: skip
                self.event(now, failure_id, run_id, "detected", et, 0, http_status=item.get("http_status"),
                           detail=detail, retry_in_s=delay, next_retry_at=next_retry)  # fmt: skip
                entry = {"failure_id": failure_id, "error_type": et.name, "kind": et.kind, "scope": scope,
                         "status": status, "next_retry_at": next_retry}  # fmt: skip
                if status == "dead":
                    self.event(now, failure_id, run_id, "dead_lettered", et, 0, detail="not retryable: needs a person")
                    dead.append({**entry, "detail": detail, "severity": et.severity, "runbook": et.runbook, "attempts": 0})
                created.append(entry)
        return {"created": created, "repeated": repeated, "dead_on_arrival": dead}

    def list_failures(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where, params = ("WHERE status = ?", [status]) if status else ("", [])
        sql = f"SELECT * FROM failure_queue {where} ORDER BY first_seen_at DESC, failure_id LIMIT ?"
        return [decode_payload(r) for r in self._all(sql, [*params, limit])]

    def failure_events(self, failure_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM failure_events WHERE failure_id = ? ORDER BY event_id", [failure_id])

    def requeue(self, failure_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Give a dead failure a fresh retry budget, for example after the cause was fixed."""
        now = now or self.clock()
        with self.lock:
            row = self._one("SELECT * FROM failure_queue WHERE failure_id = ?", [failure_id])
            if row is None:
                raise LookupError(f"no failure {failure_id}")
            if row["status"] != "dead":
                raise ValueError(f"failure {failure_id} is {row['status']}; only dead failures can be requeued")
            self._run(
                "UPDATE failure_queue SET status = 'pending', attempts = 0, next_retry_at = ?, dead_at = NULL,"
                " lease_until = NULL WHERE failure_id = ?",
                [now, failure_id],
            )
            self.event(now, failure_id, row["run_id"], "requeued", self.error_type(row["error_type"]), 0,
                       detail="requeued by an operator", next_retry_at=now)  # fmt: skip
        return {"failure_id": failure_id, "status": "pending", "next_retry_at": now}
