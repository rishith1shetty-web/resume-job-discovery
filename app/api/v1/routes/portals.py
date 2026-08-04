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
from app.schemas.processing import (
    ProcessingPreviewRequest,
    ProcessingPreviewResponse,
)
from app.services import job_processing, portal_probe

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


@router.post(
    "/{slug}/processing-preview",
    response_model=ResponseEnvelope[ProcessingPreviewResponse],
    summary="Preview how collected listings would be processed",
    description=(
        "Collects a small sample from one portal and shows what normalisation "
        "and deduplication would produce from it.\n\n"
        "**Writes nothing.** Only SELECT statements are issued: no job, source, "
        "search run or application row is created or modified. It answers "
        "\"what would processing decide?\" so the decisions can be inspected "
        "before anything is stored.\n\n"
        "Each listing reports its normalised fields, the most direct usable "
        "link for applying by hand, and the duplicate verdict with the evidence "
        "behind it. Listings sharing a `group_index` would become one job; "
        "`existing_job_id` means it would attach to a job already stored.\n\n"
        "Scoring against a resume, the match threshold, ranking and quotas are "
        "later phases and play no part here."
    ),
    responses={
        404: {"description": "No portal registered with that slug."},
        422: {"description": "Portal has no adapter, or no query was given."},
    },
)
async def preview_processing(
    slug: str,
    payload: ProcessingPreviewRequest,
    session: DbSession,
) -> ResponseEnvelope[ProcessingPreviewResponse]:
    probe = await portal_probe.probe_portal(
        session,
        slug=slug,
        queries=payload.queries,
        limit=payload.limit,
        window=payload.window,
        location=payload.location,
    )

    if not probe.ok:
        return ResponseEnvelope[ProcessingPreviewResponse](
            data=ProcessingPreviewResponse.from_results(probe=probe, preview=None),
            message="The portal did not return listings. See error_message.",
        )

    preview = await job_processing.preview_raw_jobs(
        session, portal_slug=slug, raw_jobs=probe.jobs
    )

    message = None
    if not preview.jobs:
        message = "The adapter ran but nothing survived normalisation."

    return ResponseEnvelope[ProcessingPreviewResponse](
        data=ProcessingPreviewResponse.from_results(probe=probe, preview=preview),
        message=message,
    )
