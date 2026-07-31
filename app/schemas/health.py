"""Health-check payloads."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class DependencyStatus(StrEnum):
    UP = "up"
    DOWN = "down"
    NOT_CONFIGURED = "not_configured"


class DependencyCheck(BaseModel):
    name: str
    status: DependencyStatus
    latency_ms: float | None = Field(
        default=None, description="Round-trip time of the probe, when measured."
    )
    detail: str | None = None


class HealthResponse(BaseModel):
    """Returned by ``GET /api/v1/health``.

    HTTP 200 when every required dependency is up, HTTP 503 when the overall
    status is ``degraded``, so uptime monitors can rely on the status code
    alone while the body explains which dependency failed.
    """

    status: HealthStatus
    app_name: str
    version: str
    environment: str
    api_version: str = "v1"
    timestamp: datetime
    uptime_seconds: float
    checks: list[DependencyCheck] = Field(default_factory=list)
