"""Connection, policy and the plumbing every store operation shares."""

from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import duckdb

from lab.paths import SQL_DIR
from lab.queue_api.backoff import next_delay
from lab.taxonomy import ErrorType, Taxonomy, load_taxonomy

RUN_COLUMNS = [
    "run_id", "workflow", "trigger_kind", "started_at", "finished_at", "status", "source_status",
    "rows_in", "rows_ok", "rows_failed", "duration_ms", "n8n_execution_id", "note",
]  # fmt: skip


def utcnow() -> datetime:
    """Naive UTC: every timestamp in the warehouse is UTC without a zone."""
    return datetime.now(UTC).replace(tzinfo=None)


class UnknownErrorType(ValueError):
    """An error type that is not in the taxonomy was sent to the queue."""


@dataclass
class Policy:
    """Knobs about time and tolerance, as opposed to the per-error-type retry budget in the taxonomy."""

    time_scale: float = 1.0  # multiplies retry delays and alert cool-downs; 0.01 makes a demo take seconds
    jitter: str = "equal"  # none | equal | full
    lease_s: float = 300.0  # how long a claimed failure stays locked to one worker
    cooldown_s: dict[str, float] = field(default_factory=lambda: {"info": 3600.0, "warning": 3600.0, "critical": 900.0})
    audit_max_jump: float = 0.05  # a rate that moves more than 5% between observations is suspicious
    audit_max_share: float = 0.25  # hold the batch when more than a quarter of the checked rates are suspicious


class StoreBase:
    def __init__(
        self,
        path: str = ":memory:",
        *,
        taxonomy: Taxonomy | None = None,
        policy: Policy | None = None,
        clock=utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self.con = duckdb.connect(path)
        self.con.execute("SET enable_external_access = false")  # SQL cannot read or write files: only the open database
        self.tax = taxonomy or load_taxonomy()
        self.policy = policy or Policy()
        self.clock = clock
        self.rng = rng or random.Random()
        self.lock = threading.RLock()
        self.con.execute((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
        self.sync_taxonomy()

    def _all(self, sql: str, params=()) -> list[dict[str, Any]]:
        cursor = self.con.execute(sql, list(params))
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def _one(self, sql: str, params=()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def _run(self, sql: str, params=()) -> None:
        self.con.execute(sql, list(params))

    def error_type(self, name: str) -> ErrorType:
        try:
            return self.tax[name]
        except KeyError:
            raise UnknownErrorType(f"unknown error_type {name!r}; see config/error_taxonomy.yaml") from None

    def sync_taxonomy(self) -> None:
        with self.lock:
            self._run("DELETE FROM error_types")
            for t in self.tax.values():
                self._run(
                    "INSERT INTO error_types VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [t.name, t.kind, t.retryable, t.max_attempts, t.base_s, t.cap_s, t.severity, t.runbook, t.summary],
                )

    def delay_s(self, et: ErrorType, attempt: int, retry_after_s: float | None = None) -> float:
        """Seconds until retry number attempt, with jitter, scaled by the policy's time_scale."""
        delay = next_delay(
            attempt, et.base_s, et.cap_s, jitter=self.policy.jitter, rng=self.rng, floor_s=retry_after_s or 0.0
        )
        return delay * self.policy.time_scale

    def event(self, ts, failure_id, run_id, event, et: ErrorType, attempt, *, error_type=None, **extra) -> None:
        self._run(
            "INSERT INTO failure_events (ts, failure_id, run_id, event, kind, error_type, attempt, http_status, detail,"
            " retry_in_s, next_retry_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [ts, failure_id, run_id, event, et.kind, error_type or et.name, attempt, extra.get("http_status"),
             extra.get("detail"), extra.get("retry_in_s"), extra.get("next_retry_at")],
        )  # fmt: skip

    def record_run(self, run: dict[str, Any]) -> dict[str, Any]:
        updates = ", ".join(f"{c} = excluded.{c}" for c in RUN_COLUMNS[1:])
        with self.lock:
            self._run(
                f"INSERT INTO runs ({', '.join(RUN_COLUMNS)}) VALUES ({', '.join('?' * len(RUN_COLUMNS))})"
                f" ON CONFLICT (run_id) DO UPDATE SET {updates}",
                [run.get(c) for c in RUN_COLUMNS],
            )
        return {"run_id": run["run_id"], "status": run["status"]}

    def select(self, sql: str, limit: int = 1000) -> dict[str, Any]:
        """Ad-hoc read-only SQL for operators: exactly one SELECT (or WITH ... SELECT) statement, nothing else."""
        text = sql.strip().rstrip(";").strip()
        if not text:
            raise ValueError("empty query")
        with self.lock:
            checked = json.loads(self._one("SELECT json_serialize_sql(?) AS j", [text])["j"])
            if checked.get("error"):
                raise ValueError("only a single SELECT statement is allowed: " + str(checked.get("error_message", ""))[:120])
            cursor = self.con.execute(f"SELECT * FROM ({text}) LIMIT {int(limit)}")
            columns = [d[0] for d in cursor.description]
            return {"columns": columns, "rows": [list(r) for r in cursor.fetchall()]}

    def reset(self) -> None:
        """Empty every table except the taxonomy. Lab and test use only."""
        with self.lock:
            for table in ("runs", "failure_queue", "failure_events", "fx_rates_log", "batches", "alerts"):
                self._run(f"DELETE FROM {table}")
