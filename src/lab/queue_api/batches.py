"""Write-audit-publish: stage rows, audit them against what is already published, then publish or hold."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from lab.queue_api.retries import RetryStore

COUNT_SQL = """
SELECT count(*) FILTER (WHERE c.rate IS NULL) AS rows_new,
       count(*) FILTER (WHERE c.rate IS NOT NULL AND abs(c.rate - s.rate) >= 1e-9) AS rows_changed,
       count(*) FILTER (WHERE c.rate IS NOT NULL AND abs(c.rate - s.rate) < 1e-9) AS rows_unchanged
FROM fx_rates_log s
LEFT JOIN fx_rates c ON c.obs_date = s.obs_date AND c.series = s.series
WHERE s.batch_id = ?
"""

DROP_UNCHANGED_SQL = """
DELETE FROM fx_rates_log
WHERE batch_id = ? AND EXISTS (
    SELECT 1 FROM fx_rates c
    WHERE c.obs_date = fx_rates_log.obs_date AND c.series = fx_rates_log.series AND abs(c.rate - fx_rates_log.rate) < 1e-9
)
"""

# Move of each staged rate against the observation before it, whether that one is published or staged in the same
# batch. LAG over the merged series is what makes a multi-day batch auditable.
MOVES_SQL = """
WITH staged AS (SELECT obs_date, series, rate FROM fx_rates_log WHERE batch_id = ?),
merged AS (
    SELECT obs_date, series, rate, FALSE AS is_staged FROM fx_rates r
    WHERE NOT EXISTS (SELECT 1 FROM staged s WHERE s.obs_date = r.obs_date AND s.series = r.series)
    UNION ALL
    SELECT obs_date, series, rate, TRUE FROM staged
),
moves AS (
    SELECT *, rate / NULLIF(lag(rate) OVER (PARTITION BY series ORDER BY obs_date), 0) - 1 AS move FROM merged
)
SELECT series, obs_date, rate, move FROM moves WHERE is_staged AND move IS NOT NULL ORDER BY abs(move) DESC
"""


class BatchStore(RetryStore):
    def stage_and_publish(
        self, run_id: str | None, source: str, rows: list[dict[str, Any]], now: datetime | None = None
    ) -> dict[str, Any]:
        """Stage the rows that are new or changed, audit them, then publish the batch or hold it for a person."""
        now = now or self.clock()
        result = {"batch_id": None, "outcome": "noop", "rows_new": 0, "rows_changed": 0, "rows_unchanged": 0, "audit": None}
        if not rows:
            return result
        batch_id = "b-" + uuid.uuid4().hex[:10]
        with self.lock:
            self._run(
                "INSERT INTO batches (batch_id, run_id, source, created_at, status) VALUES (?, ?, ?, ?, 'staged')",
                [batch_id, run_id, source, now],
            )
            self.con.executemany(
                "INSERT INTO fx_rates_log VALUES (?, ?, ?, ?, ?)",
                [[batch_id, r["obs_date"], r["series"], r["rate"], now] for r in rows],
            )
            counts = self._one(COUNT_SQL, [batch_id])
            self._run(DROP_UNCHANGED_SQL, [batch_id])
            if counts["rows_new"] + counts["rows_changed"] == 0:
                self._run("DELETE FROM batches WHERE batch_id = ?", [batch_id])
                return {**result, **counts}
            audit = self.audit(batch_id)
            outcome = "published" if audit["passed"] else "held"
            self._run(
                "UPDATE batches SET status = ?, rows_new = ?, rows_changed = ?, rows_unchanged = ?, audit = ?,"
                " decided_at = ?, reason = ? WHERE batch_id = ?",
                [outcome, counts["rows_new"], counts["rows_changed"], counts["rows_unchanged"], json.dumps(audit), now,
                 None if audit["passed"] else audit["summary"], batch_id],
            )  # fmt: skip
        queued = None
        if outcome == "held" and source == "ingest":
            # one open item while the audit keeps failing (scope "audit"); a retried batch that is held again adds none
            failure = {"error_type": "audit_jump", "scope": "audit", "detail": audit["summary"],
                       "payload": {"batch_id": batch_id, "worst": audit["worst"]}}  # fmt: skip
            queued = self.enqueue(run_id or batch_id, [failure], now)
        return {"batch_id": batch_id, "outcome": outcome, **counts, "audit": audit, "queued": queued}

    def audit(self, batch_id: str) -> dict[str, Any]:
        moves = self._all(MOVES_SQL, [batch_id])
        suspicious = [m for m in moves if abs(m["move"]) > self.policy.audit_max_jump]
        share = len(suspicious) / len(moves) if moves else 0.0
        return {
            "checked": len(moves),
            "suspicious": len(suspicious),
            "share": round(share, 4),
            "passed": share <= self.policy.audit_max_share,
            "worst": [
                {"series": m["series"], "obs_date": str(m["obs_date"]), "move_pct": round(m["move"] * 100, 2)}
                for m in suspicious[:5]
            ],
            "summary": f"{len(suspicious)} of {len(moves)} checked rates moved more than "
            f"{self.policy.audit_max_jump:.0%} (limit {self.policy.audit_max_share:.0%})",
        }

    def approve(self, batch_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Publish a held batch after a person checked it against the source."""
        return self._decide(batch_id, "held", "published", "approved by an operator", now)

    def rollback(self, batch_id: str, reason: str = "rolled back by an operator", now: datetime | None = None) -> dict[str, Any]:
        """Hide a published batch; the previous version of every rate it replaced becomes visible again."""
        return self._decide(batch_id, "published", "rolled_back", reason, now)

    def _decide(self, batch_id: str, expected: str, new: str, reason: str, now: datetime | None) -> dict[str, Any]:
        now = now or self.clock()
        with self.lock:
            row = self._one("SELECT status FROM batches WHERE batch_id = ?", [batch_id])
            if row is None:
                raise LookupError(f"no batch {batch_id}")
            if row["status"] != expected:
                raise ValueError(f"batch {batch_id} is {row['status']}, expected {expected}")
            self._run("UPDATE batches SET status = ?, decided_at = ?, reason = ? WHERE batch_id = ?", [new, now, reason, batch_id])
        return {"batch_id": batch_id, "status": new}

    def list_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM batches ORDER BY batch_seq DESC LIMIT ?", [limit])

    def current_rates(self, series: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where, params = ("WHERE series = ?", [series]) if series else ("", [])
        return self._all(f"SELECT * FROM fx_rates {where} ORDER BY obs_date DESC, series LIMIT ?", [*params, limit])
