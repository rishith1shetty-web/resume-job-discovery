"""Health check."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from app.core.config import Settings, get_settings
from app.schemas.common import ResponseEnvelope
from app.schemas.health import (
    DependencyCheck,
    DependencyStatus,
    HealthResponse,
    HealthStatus,
)

router = APIRouter(tags=["System"])

# Set once at import; used to report process uptime.
_STARTED_AT = time.monotonic()


async def _check_database() -> DependencyCheck:
    # Imported lazily so the module can be imported without a configured DB.
    from app.db.session import ping_database

    started = time.perf_counter()
    is_up = await ping_database()
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    return DependencyCheck(
        name="database",
        status=DependencyStatus.UP if is_up else DependencyStatus.DOWN,
        latency_ms=latency_ms,
        detail=None if is_up else "PostgreSQL did not respond to SELECT 1.",
    )


def _check_redis(settings: Settings) -> DependencyCheck:
    # Redis is provisioned for Phase 7 background workers. Nothing connects to
    # it yet, so it is reported as not_configured rather than probed - a fake
    # "up" here would be dishonest.
    return DependencyCheck(
        name="redis",
        status=DependencyStatus.NOT_CONFIGURED,
        detail="Reserved for background search workers (Phase 7). Not yet in use.",
    )


@router.get(
    "/health",
    response_model=ResponseEnvelope[HealthResponse],
    summary="Service health",
    description=(
        "Returns 200 when all required dependencies are up and 503 when the "
        "service is degraded. The body always lists each dependency check."
    ),
    responses={503: {"description": "One or more required dependencies are down."}},
)
async def health(response: Response) -> ResponseEnvelope[HealthResponse]:
    settings = get_settings()

    checks = [await _check_database(), _check_redis(settings)]
    # Only required dependencies affect overall status; not_configured does not.
    degraded = any(check.status is DependencyStatus.DOWN for check in checks)

    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    payload = HealthResponse(
        status=HealthStatus.DEGRADED if degraded else HealthStatus.OK,
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
        timestamp=datetime.now(UTC),
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 3),
        checks=checks,
    )

    # `success` tracks the HTTP status so the frontend's generic fetch wrapper
    # behaves consistently. Health is the one endpoint that still returns a
    # populated `data` on failure - the payload is the diagnosis.
    return ResponseEnvelope[HealthResponse](
        success=not degraded,
        data=payload,
        message="Service degraded." if degraded else None,
    )
