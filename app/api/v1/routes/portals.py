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
from app.schemas.portal import PortalSummary

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
