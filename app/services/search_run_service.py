"""Creating and reading search runs.

Creation records the run and its per-portal quotas and stops there. Nothing
here contacts a portal: adapters are Phase 4 and orchestration is Phase 7, so a
new run stays PENDING until an executor exists.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import (
    MIN_MATCH_SCORE_FLOOR,
    PortalRunStatus,
    SearchRunStatus,
)
from app.core.exceptions import ErrorDetail, NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.models.portal import Portal
from app.models.profile import ResumeProfile
from app.models.resume import Resume
from app.models.search import SearchRun, SearchRunPortal
from app.services.quota import compute_quotas

logger = get_logger(__name__)


async def _load_selected_portals(
    session: AsyncSession, portal_ids: Sequence[uuid.UUID]
) -> list[Portal]:
    """Fetch the requested portals in the order the caller listed them.

    Ordering matters: the quota remainder goes to the first portals, so a
    stable order keeps run creation reproducible.
    """
    result = await session.execute(select(Portal).where(Portal.id.in_(portal_ids)))
    by_id = {portal.id: portal for portal in result.scalars().all()}

    missing = [portal_id for portal_id in portal_ids if portal_id not in by_id]
    if missing:
        raise UnprocessableError(
            message="One or more selected portals do not exist.",
            details=[
                ErrorDetail(
                    code="portal_not_found",
                    field="body.selected_portal_ids",
                    message=f"Unknown portal id {portal_id}.",
                )
                for portal_id in missing
            ],
        )

    disabled = [portal for portal in by_id.values() if not portal.is_enabled]
    if disabled:
        raise UnprocessableError(
            message="One or more selected portals are not enabled.",
            details=[
                ErrorDetail(
                    code="portal_disabled",
                    field="body.selected_portal_ids",
                    message=f"Portal {portal.slug!r} is not enabled.",
                )
                for portal in disabled
            ],
        )

    return [by_id[portal_id] for portal_id in portal_ids]


async def create_search_run(
    session: AsyncSession,
    *,
    resume: Resume,
    portal_ids: Sequence[uuid.UUID],
    requested_limit: int,
    min_score: int = MIN_MATCH_SCORE_FLOOR,
) -> SearchRun:
    """Record a run and one row per selected portal, each with its quota.

    Every selected portal gets a row. None is dropped because another portal
    might satisfy the limit on its own (CLAUDE.md section 5).
    """
    if min_score < MIN_MATCH_SCORE_FLOOR:
        # Defence in depth: the schema and a CHECK constraint also refuse this.
        raise UnprocessableError(
            message="The match threshold is locked and cannot be lowered.",
            details=[
                ErrorDetail(
                    code="min_score_below_floor",
                    field="body.min_score",
                    message=f"min_score must be at least {MIN_MATCH_SCORE_FLOOR}.",
                )
            ],
        )

    portals = await _load_selected_portals(session, portal_ids)

    profile = await session.scalar(
        select(ResumeProfile).where(
            ResumeProfile.resume_id == resume.id,
            ResumeProfile.is_active.is_(True),
        )
    )
    if profile is None:
        raise UnprocessableError(
            message="Analyze the resume before searching.",
            details=[
                ErrorDetail(
                    code="profile_missing",
                    field="body.resume_id",
                    message="Run POST /api/v1/resumes/{id}/analyze first.",
                )
            ],
        )
    if profile.effective_target_role is None:
        raise UnprocessableError(
            message="No target role is set for this resume.",
            details=[
                ErrorDetail(
                    code="target_role_missing",
                    field="body.resume_id",
                    message=(
                        "Set the target role via PATCH "
                        "/api/v1/resumes/{id}/profile before searching."
                    ),
                )
            ],
        )

    run = SearchRun(
        resume_id=resume.id,
        profile_id=profile.id,
        requested_limit=requested_limit,
        min_score=min_score,
        status=SearchRunStatus.PENDING,
        # Snapshotted so re-analysing the resume cannot rewrite this run's
        # history.
        searched_domain=profile.effective_domain,
        searched_target_role=profile.effective_target_role,
        summary={},
    )
    session.add(run)
    await session.flush()

    quotas = compute_quotas(requested_limit, len(portals))
    for portal, quota in zip(portals, quotas, strict=True):
        session.add(
            SearchRunPortal(
                search_run_id=run.id,
                portal_id=portal.id,
                target_quota=quota,
                # Equal until a genuine shortage is redistributed in Phase 7.
                final_quota=quota,
                status=PortalRunStatus.PENDING,
            )
        )
    await session.flush()

    logger.info(
        "search_run_created",
        search_run_id=str(run.id),
        resume_id=str(resume.id),
        requested_limit=requested_limit,
        portal_count=len(portals),
    )
    return run


async def get_search_run(
    session: AsyncSession, *, search_run_id: uuid.UUID, user_id: uuid.UUID
) -> SearchRun:
    """Fetch one run scoped to its owner, with portal legs loaded."""
    result = await session.execute(
        select(SearchRun)
        .join(Resume, Resume.id == SearchRun.resume_id)
        .where(SearchRun.id == search_run_id, Resume.user_id == user_id)
        .options(
            selectinload(SearchRun.portals).selectinload(SearchRunPortal.portal)
        )
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise NotFoundError(
            message="Search run not found.",
            details=[
                ErrorDetail(
                    code="search_run_not_found",
                    field="path.search_run_id",
                    message=f"No search run with id {search_run_id}.",
                )
            ],
        )
    return run


async def list_search_runs(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[SearchRun], int]:
    """Page through runs, newest first, always scoped to one user."""
    filters = [Resume.user_id == user_id]
    if resume_id is not None:
        filters.append(SearchRun.resume_id == resume_id)

    total = await session.scalar(
        select(func.count())
        .select_from(SearchRun)
        .join(Resume, Resume.id == SearchRun.resume_id)
        .where(*filters)
    )

    result = await session.execute(
        select(SearchRun)
        .join(Resume, Resume.id == SearchRun.resume_id)
        .where(*filters)
        .order_by(SearchRun.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .options(
            selectinload(SearchRun.portals).selectinload(SearchRunPortal.portal)
        )
    )
    return list(result.scalars().all()), int(total or 0)
