"""The faults the stand-in source can inject. One scenario is active at a time; p is the chance a request is hit."""

from __future__ import annotations

import copy
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

KNOWN = {
    "ok", "http_500", "http_503", "http_404", "rate_limit", "timeout", "slow", "truncated", "html", "empty",
    "schema_drift", "missing_series", "null_values", "bad_type", "out_of_range", "duplicates", "stale", "jump", "bad_date",
}  # fmt: skip


@dataclass
class Scenario:
    name: str = "ok"
    p: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)
    expires_at: datetime | None = None


@dataclass
class Reply:
    status: int = 200
    body: Any = None  # a dict is sent as JSON, a str as is
    headers: dict[str, str] = field(default_factory=dict)
    delay_s: float = 0.0


def _series_ids(obs: dict[str, Any]) -> list[str]:
    return [k for k in obs if k != "d"]


def _cells(obs: list[dict[str, Any]], series: str, rows: str):
    for o in obs[-1:] if rows == "latest" else obs:
        if series in o:
            yield o


def respond(scn: Scenario, payload: dict[str, Any], rng: random.Random, today: date) -> Reply:
    """What the source answers while scn is active. payload is the healthy answer; it is copied before any damage."""
    if scn.name == "ok" or rng.random() >= scn.p:
        return Reply(body=payload)
    prm, data = scn.params, copy.deepcopy(payload)
    obs = data["observations"]
    series, rows = prm.get("series", "FXEURCAD"), prm.get("rows", "latest")
    if scn.name == "http_500":
        return Reply(500, "Internal Server Error")
    if scn.name == "http_503":
        return Reply(503, "Service Unavailable", {"Retry-After": "5"})
    if scn.name == "http_404":
        return Reply(404, {"message": "Not found"})
    if scn.name == "rate_limit":
        return Reply(429, {"message": "Too Many Requests"}, {"Retry-After": str(prm.get("retry_after_s", 30))})
    if scn.name == "timeout":
        return Reply(body=payload, delay_s=float(prm.get("hang_s", 30)))
    if scn.name == "slow":
        return Reply(body=payload, delay_s=float(prm.get("delay_s", 2)))
    if scn.name == "truncated":
        text = json.dumps(payload)
        return Reply(body=text[: len(text) // 2])
    if scn.name == "html":
        return Reply(body="<html><body>Service temporarily unavailable</body></html>")
    if scn.name == "empty":
        data["observations"] = []
    elif scn.name == "schema_drift":  # the value field is renamed in every cell
        for o in obs:
            for sid in _series_ids(o):
                o[sid] = {"value": o[sid]["v"]}
    elif scn.name == "missing_series":
        for o in obs:
            o.pop(prm.get("series", "FXCNYCAD"), None)
    elif scn.name in ("null_values", "bad_type", "out_of_range"):
        bad = {"null_values": None, "bad_type": prm.get("text", "N/A"), "out_of_range": prm.get("value", "0.0000")}[scn.name]
        for o in _cells(obs, series, rows):
            o[series] = {"v": bad}
    elif scn.name == "duplicates":  # a second copy of the newest day in which one series disagrees
        twin = copy.deepcopy(obs[-1])
        twin["FXUSDCAD"] = {"v": f"{float(twin['FXUSDCAD']['v']) * 1.001:.4f}"}
        obs.append(twin)
    elif scn.name == "jump":  # valid-looking numbers that all moved together: only an audit can catch it
        for sid in _series_ids(obs[-1]):
            obs[-1][sid] = {"v": f"{float(obs[-1][sid]['v']) * (1 + float(prm.get('pct', 0.10))):.4f}"}
    elif scn.name == "bad_date":
        obs[-1]["d"] = "2026-13-45"
    return Reply(body=data)
