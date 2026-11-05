"""Request bodies for the HTTP API. Field names are what the n8n workflows send."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field


def naive_utc(value: datetime) -> datetime:
    """The warehouse keeps UTC without a zone; n8n sends ISO strings that end in Z."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


Utc = Annotated[datetime, AfterValidator(naive_utc)]


class FailureIn(BaseModel):
    error_type: str
    scope: str = Field(description="what failed: source, dataset, or SERIES@date")
    detail: str = ""
    http_status: int | None = None
    retry_after_s: float | None = None
    payload: dict[str, Any] | None = None


class EnqueueIn(BaseModel):
    run_id: str
    failures: list[FailureIn]


class ClaimIn(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)
    lease_s: float | None = None


class ResultIn(BaseModel):
    failure_id: str
    ok: bool
    error_type: str | None = None
    detail: str = ""
    http_status: int | None = None
    retry_after_s: float | None = None


class ResultsIn(BaseModel):
    run_id: str | None = None
    results: list[ResultIn]


class RateIn(BaseModel):
    obs_date: date
    series: str
    rate: float


class BatchIn(BaseModel):
    run_id: str | None = None
    source: Literal["ingest", "retry"] = "ingest"
    rows: list[RateIn]


class ReasonIn(BaseModel):
    reason: str = "rolled back by an operator"


class RunIn(BaseModel):
    run_id: str
    workflow: Literal["ingest", "retry", "monitor"]
    trigger_kind: str | None = None
    started_at: Utc
    finished_at: Utc | None = None
    status: Literal["success", "partial", "failed", "held", "idle"]
    source_status: int | None = None
    rows_in: int = 0
    rows_ok: int = 0
    rows_failed: int = 0
    duration_ms: int | None = None
    n8n_execution_id: str | None = None
    note: str | None = None


class AlertIn(BaseModel):
    key: str
    severity: Literal["info", "warning", "critical"]
    subject: str
    body: str = ""
    run_id: str | None = None
    failure_id: str | None = None


class QueryIn(BaseModel):
    sql: str
    limit: int = Field(default=200, ge=1, le=1000)


class OutageIn(BaseModel):
    seconds: float = Field(gt=0, le=3600, description="answer 503 to every /v1 call for this long")


class PolicyIn(BaseModel):
    time_scale: float | None = Field(default=None, gt=0)
    jitter: Literal["none", "equal", "full"] | None = None
