"""Portal listing.

Read-only. The dashboard needs these to render the portal checkboxes before a
search run can be created. Enabling, disabling and configuring portals belongs
to Phase 4, when there are adapters to enable.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DbSession
from app.models.portal import Portal
from app.schemas.common import Page, PaginationMeta, ResponseEnvelope
from app.schemas.portal import (
    PortalProbeRequest,
    PortalProbeResponse,
    PortalSummary,
)
from app.services import portal_probe

router = APIRouter(prefix="/portals", tags=["Portals"])


@router.get(
    "",
    response_model=ResponseEnvelope[Page[PortalSummary]],
    summary="List portals",
    description=(
        "Portals available for selection, in display order.\n\n"
        "The portal list is data, not a fixed list in code: portals can be "
        "added, enabled or disabled without a release. A portal is only "
        "selectable once it is enabled *and* backed by an implemented adapter, "
        "which `is_selectable` reports."
    ),
)
async def list_portals(
    session: DbSession,
    enabled_only: bool = Query(
        default=False, description="Return only enabled portals."
    ),
) -> ResponseEnvelope[Page[PortalSummary]]:
    statement = select(Portal).order_by(Portal.display_order, Portal.slug)
    if enabled_only:
        statement = statement.where(Portal.is_enabled.is_(True))

    portals = list((await session.execute(statement)).scalars().all())

    return ResponseEnvelope[Page[PortalSummary]](
        data=Page[PortalSummary](
            items=[PortalSummary.model_validate(portal) for portal in portals],
            pagination=PaginationMeta.build(
                page=1, page_size=max(len(portals), 1), total=len(portals)
            ),
        )
    )


@router.post(
    "/{slug}/probe",
    response_model=ResponseEnvelope[PortalProbeResponse],
    summary="Exercise one portal adapter",
    description=(
        "Diagnostic endpoint: runs a single adapter against the live portal "
        "and returns the few listings it collected.\n\n"
        "**Writes nothing.** No search run is created, no jobs are stored and "
        "no application state changes. The limit is capped server-side, so "
        "this stays a light touch on the portal.\n\n"
        "A portal that refuses or fails returns `ok: false` with the reason in "
        "`error_code` and `error_message` rather than a 500 - the same "
        "isolation a real multi-portal run relies on.\n\n"
        "This is not the job search. Collection at scale, quotas, freshness "
        "windows, scoring and ranking arrive in later phases."
    ),
    responses={
        404: {"description": "No portal registered with that slug."},
        422: {"description": "Portal has no adapter, or no query was given."},
    },
)
async def probe_portal(
    slug: str,
    payload: PortalProbeRequest,
    session: DbSession,
) -> ResponseEnvelope[PortalProbeResponse]:
    result = await portal_probe.probe_portal(
        session,
        slug=slug,
        queries=payload.queries,
        limit=payload.limit,
        window=payload.window,
        location=payload.location,
    )

    message = None
    if not result.ok:
        message = "The portal did not return listings. See error_message."
    elif not result.jobs:
        message = "The adapter ran but the portal returned no matching listings."

    return ResponseEnvelope[PortalProbeResponse](
        data=PortalProbeResponse.from_result(result), message=message
    )
