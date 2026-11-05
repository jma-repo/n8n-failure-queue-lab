"""Alerts with a cool-down, run history and the health summary the monitor workflow reads."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from lab.queue_api.core import StoreBase


def _minutes(now: datetime, then: datetime | None) -> float | None:
    return None if then is None else round((now - then).total_seconds() / 60, 1)


class MonitorStore(StoreBase):
    def post_alert(
        self,
        key: str,
        severity: str,
        subject: str,
        body: str = "",
        *,
        run_id: str | None = None,
        failure_id: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record an alert and say whether to send it: the same key is muted for a cool-down after it was sent."""
        now = now or self.clock()
        cooldown = self.policy.cooldown_s.get(severity, 3600.0) * self.policy.time_scale
        with self.lock:
            last = self._one("SELECT max(ts) AS ts FROM alerts WHERE alert_key = ? AND sent", [key])["ts"]
            muted = last is not None and (now - last).total_seconds() < cooldown
            reason = f"same alert sent {int((now - last).total_seconds())}s ago, cool-down {int(cooldown)}s" if muted else None
            row = self._one(
                "INSERT INTO alerts (ts, alert_key, severity, subject, body, run_id, failure_id, sent, suppressed_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING alert_id",
                [now, key, severity, subject, body, run_id, failure_id, not muted, reason],
            )
        return {"alert_id": row["alert_id"], "send": not muted, "suppressed_reason": reason}

    def list_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM alerts ORDER BY ts DESC, alert_id DESC LIMIT ?", [limit])

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", [limit])

    def health(self, now: datetime | None = None) -> dict[str, Any]:
        """What the monitor workflow checks: is data still arriving, how often do runs fail, what is piling up."""
        now = now or self.clock()
        with self.lock:
            last = self._one(
                "SELECT max(finished_at) AS t FROM runs WHERE workflow = 'ingest' AND status IN ('success', 'partial')"
            )["t"]
            hour = self._one(
                "SELECT count(*) AS runs, count(*) FILTER (WHERE status IN ('failed', 'held')) AS bad_runs,"
                " coalesce(sum(rows_in), 0) AS rows_in, coalesce(sum(rows_failed), 0) AS rows_failed"
                " FROM runs WHERE workflow = 'ingest' AND started_at >= ?",
                [now - timedelta(hours=1)],
            )
            queue = {r["status"]: r["n"] for r in self._all("SELECT status, count(*) AS n FROM failure_queue GROUP BY status")}
            due = self._one("SELECT count(*) AS n FROM failure_queue WHERE status = 'pending' AND next_retry_at <= ?", [now])["n"]
            oldest = self._one("SELECT min(first_seen_at) AS t FROM failure_queue WHERE status IN ('pending', 'in_progress')")["t"]
        runs, bad_runs = int(hour["runs"]), int(hour["bad_runs"])
        return {
            "now": now,
            "last_success_at": last,
            "minutes_since_success": _minutes(now, last),
            "runs_1h": runs,
            "bad_runs_1h": bad_runs,
            "run_failure_rate_1h": round(bad_runs / runs, 4) if runs else None,
            "cell_failure_rate_1h": round(hour["rows_failed"] / hour["rows_in"], 4) if hour["rows_in"] else None,
            "queue": {s: int(queue.get(s, 0)) for s in ("pending", "in_progress", "dead", "resolved")},
            "due_now": int(due),
            "oldest_open_minutes": _minutes(now, oldest),
        }
