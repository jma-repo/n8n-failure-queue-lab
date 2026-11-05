"""Deterministic, Valet-shaped daily exchange rates: same date and series, same number, on every call."""

from __future__ import annotations

import math
import zlib
from datetime import UTC, date, datetime, timedelta
from typing import Any

# series id -> (label, rough CAD per unit); the real series have the same ids
SERIES: dict[str, tuple[str, float]] = {
    "FXUSDCAD": ("USD/CAD", 1.40),
    "FXEURCAD": ("EUR/CAD", 1.57),
    "FXGBPCAD": ("GBP/CAD", 1.80),
    "FXJPYCAD": ("JPY/CAD", 0.0092),
    "FXCNYCAD": ("CNY/CAD", 0.209),
    "FXAUDCAD": ("AUD/CAD", 0.91),
    "FXCHFCAD": ("CHF/CAD", 1.58),
    "FXMXNCAD": ("MXN/CAD", 0.075),
}
PUBLISH_HOUR_UTC = 21.5  # the Bank publishes around 16:30 Eastern


def rate(series: str, day: date) -> float:
    """A slow swing plus up to 0.4% of daily noise: moves stay far below the 5% audit threshold."""
    base = SERIES[series][1]
    phase = (zlib.crc32(series.encode()) % 628) / 100
    swing = 0.02 * math.sin(day.toordinal() / 11 + phase)
    noise = ((zlib.crc32(f"{series}{day}".encode()) % 2001) - 1000) / 1000 * 0.004
    return round(base * (1 + swing + noise), 4)


def latest_published(now: datetime) -> date:
    """Newest business day that is already out: today after the publish time, otherwise the day before."""
    day = now.date()
    if now.hour + now.minute / 60 < PUBLISH_HOUR_UTC or day.weekday() >= 5:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def business_days(end: date, count: int) -> list[date]:
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return days[::-1]


def build_payload(series_ids: list[str], days: list[date]) -> dict[str, Any]:
    return {
        "terms": {"url": "https://www.bankofcanada.ca/terms/"},
        "seriesDetail": {
            sid: {"label": SERIES[sid][0], "dimension": {"key": "d", "name": "Date"}} for sid in series_ids
        },
        "observations": [
            {"d": d.isoformat(), **{sid: {"v": f"{rate(sid, d):.4f}"} for sid in series_ids}} for d in days
        ],
    }


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
