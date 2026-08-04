"""Exercise one portal adapter safely.

A deliberately small, read-only path for checking that an adapter still works
against the real portal: it runs one adapter, with a hard cap on how much it
asks for, and **writes nothing to the database**. No search run is created, no
jobs are stored, no application state is touched.

Its job is to answer "does this adapter still collect usable listings?" without
starting the Phase 7 orchestration that does not exist yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import FreshnessWindow
from app.core.exceptions import ErrorDetail, NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.models.portal import Portal
from app.portals.base import PortalSearchRequest, RawJob
from app.portals.exceptions import PortalError
from app.portals.registry import PortalRegistry, registry as default_registry

logger = get_logger(__name__)

#: A probe is a smoke test, not a collection run. Kept small on purpose so it
#: is a light touch on the portal.
MAX_PROBE_LIMIT = 10
MAX_PROBE_QUERIES = 3


@dataclass(slots=True)
class ProbeResult:
    """Outcome of one adapter probe. Nothing here is persisted."""

    portal_slug: str
    ok: bool
    queries: list[str]
    jobs: list[RawJob] = field(default_factory=list)
    duration_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    health_available: bool | None = None
    health_detail: str | None = None


async def probe_portal(
    session: AsyncSession,
    *,
    slug: str,
    queries: list[str],
    limit: int = 5,
    window: FreshnessWindow = FreshnessWindow.LAST_7D,
    location: str | None = None,
    portal_registry: PortalRegistry | None = None,
) -> ProbeResult:
    """Run one adapter against the live portal and return what it collected.

    Failure is captured, not raised: a probe reporting "this portal is blocked"
    is a successful diagnostic, and it mirrors how a real run isolates one
    portal's failure from the rest.
    """
    portal_registry = portal_registry or default_registry

    portal = await session.scalar(
        select(Portal).where(Portal.slug == slug).options(selectinload(Portal.config))
    )
    if portal is None:
        raise NotFoundError(
            message="Portal not found.",
            details=[
                ErrorDetail(
                    code="portal_not_found",
                    field="path.slug",
                    message=f"No portal registered with slug {slug!r}.",
                )
            ],
        )

    if not portal_registry.is_registered(slug):
        raise UnprocessableError(
            message="This portal has no adapter yet.",
            details=[
                ErrorDetail(
                    code="portal_not_implemented",
                    field="path.slug",
                    message=(
                        f"Portal {slug!r} exists but no adapter is registered "
                        "for it."
                    ),
                )
            ],
        )

    trimmed_queries = [q.strip() for q in queries if q and q.strip()][
        :MAX_PROBE_QUERIES
    ]
    if not trimmed_queries:
        raise UnprocessableError(
            message="At least one query is required.",
            details=[
                ErrorDetail(
                    code="queries_required",
                    field="body.queries",
                    message="Provide one or more search titles.",
                )
            ],
        )

    capped_limit = max(1, min(limit, MAX_PROBE_LIMIT))
    options: dict[str, Any] = dict(portal.config.options) if portal.config else {}
    # Operational policy comes from the portal's own configuration row.
    if portal.config is not None:
        options.setdefault("timeout_seconds", portal.config.timeout_seconds)
        options.setdefault("max_retries", portal.config.max_retries)
        options.setdefault("retry_backoff_seconds", portal.config.retry_backoff_seconds)
        options.setdefault("rate_limit_per_minute", portal.config.rate_limit_per_minute)

    adapter = portal_registry.create(slug, options)
    started = time.monotonic()

    health = None
    try:
        health = await adapter.health_check()
    except PortalError as exc:
        logger.info("portal_probe_health_failed", portal=slug, code=exc.code)

    try:
        result = await adapter.search(
            PortalSearchRequest(
                queries=tuple(trimmed_queries),
                window=window,
                limit=capped_limit,
                location=location,
            )
        )
    except PortalError as exc:
        # Exactly how a real run treats it: recorded against this portal,
        # never allowed to take down anything else.
        logger.info(
            "portal_probe_failed", portal=slug, code=exc.code, message=exc.message
        )
        return ProbeResult(
            portal_slug=slug,
            ok=False,
            queries=trimmed_queries,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_code=exc.code,
            error_message=exc.message,
            health_available=health.is_available if health else None,
            health_detail=health.detail if health else None,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "portal_probe_completed",
        portal=slug,
        collected=len(result.jobs),
        duration_ms=duration_ms,
    )

    return ProbeResult(
        portal_slug=slug,
        ok=True,
        queries=trimmed_queries,
        jobs=result.jobs[:capped_limit],
        duration_ms=duration_ms,
        health_available=health.is_available if health else None,
        health_detail=health.detail if health else None,
    )
