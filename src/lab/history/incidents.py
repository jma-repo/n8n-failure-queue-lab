"""The invented incidents the RCA queries have to find. The SQL never reads this file; the tests do."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

START = datetime(2026, 10, 12, 0, 0)  # a Monday, UTC
DAYS = 14
BLIP_P = 0.004  # chance that any single request fails for no reason: background noise


@dataclass(frozen=True)
class Incident:
    key: str
    fault: str  # the error type a run sees while it lasts
    start: datetime
    end: datetime
    p: float = 1.0  # chance that a request inside the window is hit
    daily: bool = False  # repeats at the same time of day on every day of the history
    why: str = ""

    def covers(self, t: datetime) -> bool:
        if self.daily:
            seconds = t.hour * 3600 + t.minute * 60
            return self.start.hour * 3600 + self.start.minute * 60 <= seconds < self.end.hour * 3600 + self.end.minute * 60
        return self.start <= t < self.end


def at(day: int, hh: int, mm: int = 0) -> datetime:
    return START + timedelta(days=day, hours=hh, minutes=mm)


INCIDENTS = [
    Incident("nightly-maintenance", "timeout", at(0, 7), at(0, 7, 30), p=0.9, daily=True, why="the source is slow every morning from 07:00 to 07:30 UTC"),
    Incident("upstream-outage", "http_5xx", at(2, 15, 20), at(2, 18, 50), why="the source returns 503 for three and a half hours"),
    Incident("schema-drift", "schema_drift", at(5, 17, 20), at(6, 11, 0), why="the source renamed the value field v to value"),
    Incident("null-cny", "null_value", at(8, 4), at(8, 9), why="the source sends null for the CNY rate in the newest row"),
    Incident("rate-limit", "rate_limited", at(10, 9), at(10, 9, 20), why="the source answers 429 with Retry-After 600"),
    Incident("restated-rates", "audit_jump", at(11, 14, 15), at(11, 14, 50), why="the source restates yesterday's rates 8% higher, then reverts"),
]  # fmt: skip

# a person acts: (when, what). They requeue what retrying gave up on, once the cause is fixed.
HUMAN_ACTIONS = [
    (at(2, 19, 5), "requeue_dead"),
    (at(6, 11, 10), "requeue_dead"),
]


def fault_at(t: datetime, rng) -> str | None:
    """The fault a request made at t meets, or None. Draws from rng in a fixed order so a seed repeats the same history."""
    for incident in INCIDENTS:
        if incident.covers(t) and rng.random() < incident.p:
            return incident.fault
    return "http_5xx" if rng.random() < BLIP_P else None
