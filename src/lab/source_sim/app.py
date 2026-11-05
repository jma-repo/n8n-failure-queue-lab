"""HTTP face of the stand-in source: a Valet-compatible endpoint plus an admin switch for faults."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field

from lab.source_sim import data
from lab.source_sim.scenarios import KNOWN, Reply, Scenario, respond


class ScenarioIn(BaseModel):
    name: str
    p: float = Field(default=1.0, ge=0, le=1)
    params: dict[str, Any] = Field(default_factory=dict)
    ttl_s: float | None = Field(default=None, gt=0, description="go back to ok after this many seconds")
    seed: int | None = None


def to_response(reply: Reply) -> Response:
    if isinstance(reply.body, dict):
        content, media = json.dumps(reply.body), "application/json"
    else:
        content = reply.body or ""
        media = "text/html" if content.startswith("<") else "application/json"
    return Response(content=content, status_code=reply.status, media_type=media, headers=reply.headers)


def create_app(*, clock=data.utcnow, sleeper=asyncio.sleep) -> FastAPI:
    api = FastAPI(title="Valet stand-in", version="0.1.0")
    state: dict[str, Any] = {"scenario": Scenario(), "rng": random.Random(), "requests": 0, "hits": 0}

    def active() -> Scenario:
        scn = state["scenario"]
        if scn.expires_at and clock() >= scn.expires_at:
            state["scenario"] = scn = Scenario()
        return scn

    def describe() -> dict[str, Any]:
        scn = active()
        return {"name": scn.name, "p": scn.p, "params": scn.params, "requests": state["requests"], "hits": state["hits"]}

    @api.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/valet/observations/{series}/json")
    async def observations(
        series: str,
        recent: int | None = Query(default=None, ge=1, le=100),
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> Response:
        ids = [s.strip() for s in series.split(",") if s.strip()]
        for sid in ids:
            if sid not in data.SERIES:
                return Response(json.dumps({"message": f"Series {sid} not found."}), 404, media_type="application/json")
        now = clock()
        scn = active()
        as_of = now - timedelta(days=int(scn.params.get("days", 7))) if scn.name == "stale" else now
        newest = min(end_date or date.max, data.latest_published(as_of))
        if start_date:
            days = [d for d in data.business_days(newest, 120) if d >= start_date]
        else:
            days = data.business_days(newest, recent or 10)
        state["requests"] += 1
        first_n = scn.params.get("first_n")
        if scn.name != "ok":
            state["hits"] += 1
            if first_n is not None and state["hits"] > int(first_n):
                scn = Scenario()
        reply = respond(scn, data.build_payload(ids, days), state["rng"], now.date())
        if reply.delay_s:
            await sleeper(reply.delay_s)
        return to_response(reply)

    @api.post("/admin/scenario")
    def set_scenario(body: ScenarioIn) -> dict[str, Any]:
        if body.name not in KNOWN:
            raise HTTPException(422, f"unknown scenario {body.name}; choose from {sorted(KNOWN)}")
        expires = clock() + timedelta(seconds=body.ttl_s) if body.ttl_s else None
        state.update(scenario=Scenario(body.name, body.p, body.params, expires), hits=0)
        state["rng"] = random.Random(body.seed) if body.seed is not None else random.Random()
        return describe()

    @api.get("/admin/scenario")
    def get_scenario() -> dict[str, Any]:
        return describe()

    @api.delete("/admin/scenario")
    def clear_scenario() -> dict[str, Any]:
        state.update(scenario=Scenario(), hits=0, requests=0)
        return describe()

    return api
