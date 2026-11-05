"""HTTP API over the store. n8n talks to this; the store is the same one the history simulator drives."""

from __future__ import annotations

import os
import secrets
from datetime import date, datetime, timedelta
from typing import Any

import duckdb
from fastapi import Depends, FastAPI, Header, HTTPException

from lab.queue_api import models as m
from lab.queue_api.store import Policy, Store, UnknownErrorType


def wire(value: Any) -> Any:
    """JSON-ready: timestamps leave as ISO strings ending in Z (they are UTC), dates as plain ISO."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds") + "Z"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: wire(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [wire(v) for v in value]
    return value


def store_from_env() -> Store:
    policy = Policy(
        time_scale=float(os.environ.get("LAB_TIME_SCALE", "1")),
        jitter=os.environ.get("LAB_JITTER", "equal"),
    )
    return Store(os.environ.get("LAB_DB_PATH", "/data/lab.duckdb"), policy=policy)


def create_app(store: Store | None = None, *, token: str | None = None, allow_admin: bool | None = None) -> FastAPI:
    store = store or store_from_env()
    token = os.environ.get("LAB_API_TOKEN", "") if token is None else token
    if allow_admin is None:
        allow_admin = os.environ.get("LAB_ALLOW_ADMIN") == "1"
    api = FastAPI(title="Failure queue API", version="0.1.0")

    def auth(x_api_key: str | None = Header(default=None)) -> None:
        if token and not (x_api_key and secrets.compare_digest(x_api_key, token)):
            raise HTTPException(401, "missing or wrong X-API-Key")

    outage = {"until": None}

    def available() -> None:
        until = outage["until"]
        if until is not None and store.clock() < until:
            raise HTTPException(503, "simulated outage of the queue API")

    guarded = [Depends(auth), Depends(available)]
    admin_only = [Depends(auth)]

    @api.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.post("/v1/failures", dependencies=guarded)
    def enqueue(body: m.EnqueueIn):
        try:
            return wire(store.enqueue(body.run_id, [f.model_dump() for f in body.failures]))
        except UnknownErrorType as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.get("/v1/failures", dependencies=guarded)
    def list_failures(status: str | None = None, limit: int = 100):
        return wire({"items": store.list_failures(status, limit)})

    @api.get("/v1/failures/{failure_id}/events", dependencies=guarded)
    def failure_events(failure_id: str):
        return wire({"items": store.failure_events(failure_id)})

    @api.post("/v1/failures/claim", dependencies=guarded)
    def claim(body: m.ClaimIn):
        return wire({"items": store.claim(body.limit, body.lease_s)})

    @api.post("/v1/failures/results", dependencies=guarded)
    def results(body: m.ResultsIn):
        return wire(store.apply_results(body.run_id, [r.model_dump() for r in body.results]))

    @api.post("/v1/failures/{failure_id}/requeue", dependencies=guarded)
    def requeue(failure_id: str):
        try:
            return wire(store.requeue(failure_id))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/v1/batches", dependencies=guarded)
    def publish(body: m.BatchIn):
        return wire(store.stage_and_publish(body.run_id, body.source, [r.model_dump() for r in body.rows]))

    @api.get("/v1/batches", dependencies=guarded)
    def list_batches(limit: int = 50):
        return wire({"items": store.list_batches(limit)})

    @api.post("/v1/batches/{batch_id}/approve", dependencies=guarded)
    def approve(batch_id: str):
        return decide(store.approve, batch_id)

    @api.post("/v1/batches/{batch_id}/rollback", dependencies=guarded)
    def rollback(batch_id: str, body: m.ReasonIn | None = None):
        return decide(store.rollback, batch_id, (body or m.ReasonIn()).reason)

    def decide(action, *args):
        try:
            return wire(action(*args))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/v1/rates/current", dependencies=guarded)
    def rates(series: str | None = None, limit: int = 50):
        return wire({"items": store.current_rates(series, limit)})

    @api.post("/v1/runs", dependencies=guarded)
    def record_run(body: m.RunIn):
        return wire(store.record_run(body.model_dump()))

    @api.get("/v1/runs", dependencies=guarded)
    def runs(limit: int = 50):
        return wire({"items": store.list_runs(limit)})

    @api.post("/v1/alerts", dependencies=guarded)
    def post_alert(body: m.AlertIn):
        return wire(
            store.post_alert(body.key, body.severity, body.subject, body.body, run_id=body.run_id, failure_id=body.failure_id)
        )

    @api.get("/v1/alerts", dependencies=guarded)
    def alerts(limit: int = 50):
        return wire({"items": store.list_alerts(limit)})

    @api.get("/v1/health/summary", dependencies=guarded)
    def health():
        return wire(store.health())

    if allow_admin:

        @api.post("/admin/reset", dependencies=admin_only)
        def reset():
            store.reset()
            return {"status": "empty"}

        @api.post("/admin/query", dependencies=admin_only)
        def query(body: m.QueryIn):
            try:
                return wire(store.select(body.sql, body.limit))
            except (ValueError, duckdb.Error) as exc:
                raise HTTPException(400, str(exc)) from exc

        @api.post("/admin/outage", dependencies=admin_only)
        def start_outage(body: m.OutageIn):
            outage["until"] = store.clock() + timedelta(seconds=body.seconds)
            return {"until": wire(outage["until"])}

        @api.delete("/admin/outage", dependencies=admin_only)
        def end_outage():
            outage["until"] = None
            return {"until": None}

        @api.post("/admin/policy", dependencies=admin_only)
        def policy(body: m.PolicyIn):
            if body.time_scale is not None:
                store.policy.time_scale = body.time_scale
            if body.jitter is not None:
                store.policy.jitter = body.jitter
            return {"time_scale": store.policy.time_scale, "jitter": store.policy.jitter}

    return api
