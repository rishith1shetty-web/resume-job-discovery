"""Search-run endpoints.

Creating a run records what to search and the quota each selected portal gets.
It does not search: portal adapters land in Phase 4 and orchestration in Phase
7, so a run created here stays PENDING and its counters stay zero. The contract
is fixed now so the frontend can be built against it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.core.constants import PortalRunStatus
from app.models.search import SearchRun, SearchRunPortal
from app.schemas.common import Page, PaginationMeta, ResponseEnvelope
from app.schemas.search import (
    SearchRunCreate,
    SearchRunOut,
    SearchRunPortalOut,
    SearchRunProgressOut,
)
from app.services import resume_service, search_run_service

router = APIRouter(prefix="/search-runs", tags=["Search"])


def _portal_out(leg: SearchRunPortal) -> SearchRunPortalOut:
    portal = leg.portal
    return SearchRunPortalOut(
        id=leg.id,
        portal_id=leg.portal_id,
        portal_slug=portal.slug if portal else None,
        portal_display_name=portal.display_name if portal else None,
        target_quota=leg.target_quota,
        final_quota=leg.final_quota,
        raw_count=leg.raw_count,
        qualified_count=leg.qualified_count,
        selected_count=leg.selected_count,
        shortfall=leg.shortfall,
        status=leg.status,
        error=leg.error,
        deepest_window=leg.deepest_window,
        started_at=leg.started_at,
        completed_at=leg.completed_at,
    )


def _run_out(run: SearchRun) -> SearchRunOut:
    return SearchRunOut(
        id=run.id,
        resume_id=run.resume_id,
        profile_id=run.profile_id,
        requested_limit=run.requested_limit,
        min_score=run.min_score,
        status=run.status,
        searched_domain=run.searched_domain,
        searched_target_role=run.searched_target_role,
        started_at=run.started_at,
        completed_at=run.completed_at,
        error_message=run.error_message,
        summary=dict(run.summary or {}),
        created_at=run.created_at,
        portals=[_portal_out(leg) for leg in run.portals],
    )


@router.post(
    "",
    response_model=ResponseEnvelope[SearchRunOut],
    status_code=status.HTTP_201_CREATED,
    summary="Create a search run",
    description=(
        "Records a search of one resume across the selected portals and "
        "computes each portal's quota.\n\n"
        "`requested_limit` accepts only 20, 50 or 100, and is a target for "
        "*qualified* jobs. It is split as evenly as possible over the selected "
        "portals; the remainder goes to the first portals listed. Every "
        "selected portal is searched - the run does not stop once one portal "
        "has produced enough.\n\n"
        "The 70% match threshold is locked and is not accepted as input.\n\n"
        "**Not yet executed.** Portal adapters arrive in Phase 4 and the "
        "background runner in Phase 7, so the run is created `pending` with "
        "zero counters."
    ),
    responses={
        404: {"description": "Resume not found."},
        422: {
            "description": (
                "Unknown or disabled portal, unanalyzed resume, no target "
                "role, or an invalid limit."
            )
        },
    },
)
async def create_search_run(
    payload: SearchRunCreate,
    session: DbSession,
    settings: AppSettings,
    user: CurrentUser,
) -> ResponseEnvelope[SearchRunOut]:
    resume = await resume_service.get_resume(
        session, resume_id=payload.resume_id, user_id=user.id
    )

    run = await search_run_service.create_search_run(
        session,
        resume=resume,
        portal_ids=payload.selected_portal_ids,
        requested_limit=payload.requested_limit,
        min_score=settings.min_match_score,
    )
    await session.commit()

    # Re-read so the portal legs come back with their portal rows attached.
    created = await search_run_service.get_search_run(
        session, search_run_id=run.id, user_id=user.id
    )

    return ResponseEnvelope[SearchRunOut](
        data=_run_out(created),
        message="Search run created. Collection runs in a later phase.",
    )


@router.get(
    "",
    response_model=ResponseEnvelope[Page[SearchRunOut]],
    summary="List search runs",
    description="Newest first. Filter by resume to see one resume's history.",
)
async def list_search_runs(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> ResponseEnvelope[Page[SearchRunOut]]:
    runs, total = await search_run_service.list_search_runs(
        session,
        user_id=user.id,
        resume_id=resume_id,
        page=page,
        page_size=page_size,
    )

    return ResponseEnvelope[Page[SearchRunOut]](
        data=Page[SearchRunOut](
            items=[_run_out(run) for run in runs],
            pagination=PaginationMeta.build(
                page=page, page_size=page_size, total=total
            ),
        )
    )


@router.get(
    "/{search_run_id}",
    response_model=ResponseEnvelope[SearchRunOut],
    summary="Get a search run",
    responses={404: {"description": "Search run not found."}},
)
async def get_search_run(
    search_run_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ResponseEnvelope[SearchRunOut]:
    run = await search_run_service.get_search_run(
        session, search_run_id=search_run_id, user_id=user.id
    )
    return ResponseEnvelope[SearchRunOut](data=_run_out(run))


@router.get(
    "/{search_run_id}/progress",
    response_model=ResponseEnvelope[SearchRunProgressOut],
    summary="Get search-run progress",
    description=(
        "Per-portal progress for the dashboard.\n\n"
        "Progress is reported per portal so a failure is visibly scoped to "
        "that portal. One portal failing does not fail the run: it finishes "
        "as `completed_with_errors`."
    ),
    responses={404: {"description": "Search run not found."}},
)
async def get_search_run_progress(
    search_run_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ResponseEnvelope[SearchRunProgressOut]:
    run = await search_run_service.get_search_run(
        session, search_run_id=search_run_id, user_id=user.id
    )
    legs = list(run.portals)

    return ResponseEnvelope[SearchRunProgressOut](
        data=SearchRunProgressOut(
            id=run.id,
            status=run.status,
            is_terminal=run.is_terminal,
            portal_count=len(legs),
            portals_completed=sum(
                1 for leg in legs if leg.status is PortalRunStatus.COMPLETED
            ),
            portals_failed=sum(
                1 for leg in legs if leg.status is PortalRunStatus.FAILED
            ),
            total_selected=sum(leg.selected_count for leg in legs),
            requested_limit=run.requested_limit,
            portals=[_portal_out(leg) for leg in legs],
        )
    )
