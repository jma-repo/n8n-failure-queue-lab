"""Claiming due failures for a worker and recording what happened to each retry."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from lab.queue_api.failures import FailureStore, decode_payload

CLAIM_SQL = """
UPDATE failure_queue SET status = 'in_progress', lease_until = ?, last_attempt_at = ?
WHERE failure_id IN (
    SELECT failure_id FROM failure_queue
    WHERE (status = 'pending' AND next_retry_at <= ?) OR (status = 'in_progress' AND lease_until < ?)
    ORDER BY next_retry_at, failure_id
    LIMIT ?
)
RETURNING failure_id, kind, error_type, scope, error_detail, payload, attempts, max_attempts, first_seen_at, run_id
"""


class RetryStore(FailureStore):
    def claim(self, limit: int = 50, lease_s: float | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
        """Lock due failures to one worker for lease_s. A lease that ran out (a crashed worker) makes the item due again."""
        now = now or self.clock()
        lease_until = now + timedelta(seconds=lease_s or self.policy.lease_s)
        with self.lock:
            rows = self._all(CLAIM_SQL, [lease_until, now, now, now, limit])
        return sorted((decode_payload(r) for r in rows), key=lambda r: (r["first_seen_at"], r["failure_id"]))

    def apply_results(
        self, run_id: str | None, results: list[dict[str, Any]], now: datetime | None = None
    ) -> dict[str, Any]:
        """Resolve, reschedule with a longer delay, or dead-letter each claimed failure. Late or repeated results are ignored."""
        now = now or self.clock()
        out: dict[str, list] = {"resolved": [], "rescheduled": [], "dead": [], "ignored": []}
        with self.lock:
            for r in results:
                row = self._one("SELECT * FROM failure_queue WHERE failure_id = ?", [r["failure_id"]])
                if row is None or row["status"] != "in_progress":
                    out["ignored"].append({"failure_id": r["failure_id"], "reason": "not in progress (late or repeated)"})
                    continue
                fid, et = row["failure_id"], self.error_type(row["error_type"])
                attempt = row["attempts"] + 1
                detail = r.get("detail") or ""
                seen = r.get("error_type") if r.get("error_type") in self.tax else None
                if r["ok"]:
                    self._run(
                        "UPDATE failure_queue SET status = 'resolved', attempts = ?, resolved_at = ?, last_attempt_at = ?,"
                        " lease_until = NULL, next_retry_at = NULL WHERE failure_id = ?",
                        [attempt, now, now, fid],
                    )
                    self.event(now, fid, run_id, "retry_ok", et, attempt, detail=detail)
                    out["resolved"].append(fid)
                elif attempt >= row["max_attempts"]:
                    self._run(
                        "UPDATE failure_queue SET status = 'dead', attempts = ?, dead_at = ?, last_attempt_at = ?,"
                        " error_detail = ?, lease_until = NULL, next_retry_at = NULL WHERE failure_id = ?",
                        [attempt, now, now, detail or row["error_detail"], fid],
                    )
                    self.event(now, fid, run_id, "retry_failed", et, attempt, error_type=seen,
                               http_status=r.get("http_status"), detail=detail)  # fmt: skip
                    self.event(now, fid, run_id, "dead_lettered", et, attempt, detail=f"gave up after {attempt} attempts")
                    out["dead"].append({
                        "failure_id": fid, "error_type": et.name, "kind": et.kind, "scope": row["scope"],
                        "detail": detail or row["error_detail"], "attempts": attempt, "first_seen_at": row["first_seen_at"],
                        "severity": et.severity, "runbook": et.runbook,
                    })  # fmt: skip
                else:
                    delay = self.delay_s(et, attempt, r.get("retry_after_s"))
                    next_retry = now + timedelta(seconds=delay)
                    self._run(
                        "UPDATE failure_queue SET status = 'pending', attempts = ?, next_retry_at = ?, last_attempt_at = ?,"
                        " error_detail = ?, lease_until = NULL WHERE failure_id = ?",
                        [attempt, next_retry, now, detail or row["error_detail"], fid],
                    )
                    self.event(now, fid, run_id, "retry_failed", et, attempt, error_type=seen,
                               http_status=r.get("http_status"), detail=detail, retry_in_s=delay, next_retry_at=next_retry)  # fmt: skip
                    out["rescheduled"].append({"failure_id": fid, "attempts": attempt, "next_retry_at": next_retry})
        return out
