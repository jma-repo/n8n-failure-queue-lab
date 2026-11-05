"""The failure-queue store: one class that composes the pieces so the HTTP layer and the simulator share it."""

from __future__ import annotations

from lab.queue_api.batches import BatchStore
from lab.queue_api.core import Policy, UnknownErrorType, utcnow


class Store(BatchStore):
    """Failure queue + write-audit-publish batches + run log + alert ledger, over one DuckDB connection."""


__all__ = ["Policy", "Store", "UnknownErrorType", "utcnow"]
