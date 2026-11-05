"""The error taxonomy (config/error_taxonomy.yaml): what each failure means and how patient the queue is with it."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from lab.paths import CONFIG_DIR

KINDS = {"fetch", "dataset", "row", "audit", "system"}
SEVERITIES = {"info", "warning", "critical"}


@dataclass(frozen=True)
class ErrorType:
    name: str
    kind: str
    retryable: bool
    max_attempts: int
    base_s: float
    cap_s: float
    severity: str
    runbook: str
    summary: str


class Taxonomy(Mapping[str, ErrorType]):
    def __init__(self, types: dict[str, ErrorType]) -> None:
        self._types = types

    def __getitem__(self, name: str) -> ErrorType:
        return self._types[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._types)

    def __len__(self) -> int:
        return len(self._types)


def _build(name: str, spec: dict) -> ErrorType:
    error_type = ErrorType(
        name=name,
        kind=spec["kind"],
        retryable=bool(spec["retryable"]),
        max_attempts=int(spec["max_attempts"]),
        base_s=float(spec["base_s"]),
        cap_s=float(spec["cap_s"]),
        severity=spec["severity"],
        runbook=spec["runbook"],
        summary=spec["summary"],
    )
    problems = []
    if error_type.kind not in KINDS:
        problems.append(f"kind {error_type.kind} is not one of {sorted(KINDS)}")
    if error_type.severity not in SEVERITIES:
        problems.append(f"severity {error_type.severity} is not one of {sorted(SEVERITIES)}")
    if error_type.max_attempts < 1:
        problems.append("max_attempts must be at least 1")
    if error_type.retryable and not 0 < error_type.base_s <= error_type.cap_s:
        problems.append("a retryable type needs 0 < base_s <= cap_s")
    if problems:
        raise ValueError(f"error type {name}: " + "; ".join(problems))
    return error_type


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    raw = yaml.safe_load((path or CONFIG_DIR / "error_taxonomy.yaml").read_text(encoding="utf-8"))["error_types"]
    return Taxonomy({name: _build(name, spec) for name, spec in raw.items()})
