"""Creating and reading search runs.

Creation records the run, validates the selection and computes each selected
portal's quota. Executing the run - collection, scoring, allocation - belongs to
``app.services.search_orchestrator``; this module prepares the inputs it needs
and reads the outcome back afterwards.

A portal is only accepted when it exists, is enabled, and has a registered
adapter. Accepting a portal with no adapter would create a leg that can only
fail, which reads to the user as "this portal is broken" rather than "this
portal is not built yet".
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import (
    MIN_MATCH_SCORE_FLOOR,
    ApplicationStatus,
    PortalRunStatus,
    SearchRunStatus,
)
from app.core.exceptions import ErrorDetail, NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.models.application import Application
from app.models.job import Job, JobMatch
from app.models.portal import Portal
from app.models.profile import ResumeProfile
from app.models.resume import Resume
from app.models.search import SearchRun, SearchRunPortal
from app.portals.registry import PortalRegistry, registry as default_registry
from app.services.quota import compute_quotas

logger = get_logger(__name__)


@dataclass(slots=True)
class PreparedRun:
    """A recorded run and the aligned inputs the orchestrator executes.

    ``legs`` and ``portals`` are positionally aligned and in the user's
    selection order - the order the quota remainder follows, and therefore the
    order that makes a run reproducible.
    """

    run: SearchRun
    legs: list[SearchRunPortal]
    portals: list[Portal]


async def _load_selected_portals(
    session: AsyncSession,
    portal_ids: Sequence[uuid.UUID],
    *,
    require_adapter: bool = True,
    portal_registry: PortalRegistry | None = None,
) -> list[Portal]:
    """Fetch the requested portals in the order the caller listed them.

    Ordering matters: the quota remainder goes to the first portals, so a
    stable order keeps run creation reproducible.

    ``require_adapter`` refuses a portal that has no registered adapter. It is
    the check that turns "unimplemented" into an actionable validation error
    instead of a portal leg that is guaranteed to fail.
    """
    result = await session.execute(
        select(Portal)
        .where(Portal.id.in_(portal_ids))
        .options(selectinload(Portal.config))
    )
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

    if require_adapter:
        active_registry = portal_registry or default_registry
        unimplemented = [
            portal
            for portal in by_id.values()
            if not portal.is_implemented
            or not active_registry.is_registered(portal.adapter_key or portal.slug)
        ]
        if unimplemented:
            raise UnprocessableError(
                message="One or more selected portals have no adapter yet.",
                details=[
                    ErrorDetail(
                        code="portal_not_implemented",
                        field="body.selected_portal_ids",
                        message=(
                            f"Portal {portal.slug!r} is registered but its "
                            "adapter has not been built, so it cannot be "
                            "searched."
                        ),
                    )
                    for portal in unimplemented
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
    require_adapter: bool = True,
    portal_registry: PortalRegistry | None = None,
) -> PreparedRun:
    """Record a run and one row per selected portal, each with its quota.

    Every selected portal gets a row. None is dropped because another portal
    might satisfy the limit on its own (CLAUDE.md section 5).

    Returns the run together with its legs and portals, aligned, so the caller
    can execute it without re-reading what it just wrote.
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

    portals = await _load_selected_portals(
        session,
        portal_ids,
        require_adapter=require_adapter,
        portal_registry=portal_registry,
    )

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
    legs: list[SearchRunPortal] = []
    for portal, quota in zip(portals, quotas, strict=True):
        leg = SearchRunPortal(
            search_run_id=run.id,
            portal_id=portal.id,
            target_quota=quota,
            # Equal until a genuine shortage is redistributed during execution.
            final_quota=quota,
            status=PortalRunStatus.PENDING,
        )
        session.add(leg)
        legs.append(leg)
    await session.flush()

    logger.info(
        "search_run_created",
        search_run_id=str(run.id),
        resume_id=str(resume.id),
        requested_limit=requested_limit,
        portal_count=len(portals),
    )
    return PreparedRun(run=run, legs=legs, portals=portals)


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


async def application_statuses(
    session: AsyncSession,
    *,
    resume_id: uuid.UUID,
    job_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, ApplicationStatus]:
    """Current application status per job, for one resume only.

    Scoped to the resume: two resumes may hold different statuses for the same
    vacancy, and neither may see the other's (CLAUDE.md section 11).
    """
    if not job_ids:
        return {}

    rows = await session.execute(
        select(Application.job_id, Application.status).where(
            Application.resume_id == resume_id,
            Application.job_id.in_(job_ids),
        )
    )
    return {job_id: status for job_id, status in rows}


@dataclass(slots=True)
class StoredResult:
    """One job from a completed run, read back after the fact."""

    job: Job
    match: JobMatch
    portal_slugs: tuple[str, ...]


async def load_run_results(
    session: AsyncSession, *, run: SearchRun
) -> list[StoredResult]:
    """Re-read a finished run's result set, in the order it was returned.

    Membership and order come from ``search_runs.summary['selected_job_ids']``,
    written when the run completed. Reading the order back rather than
    re-deriving it means a later scoring change cannot silently reshuffle a run
    that has already been shown to the user.
    """
    summary = dict(run.summary or {})
    ordered_ids: list[uuid.UUID] = []
    for raw in summary.get("selected_job_ids", []) or []:
        try:
            ordered_ids.append(uuid.UUID(str(raw)))
        except ValueError:  # pragma: no cover - defensive
            continue

    if not ordered_ids:
        return []

    jobs = {
        job.id: job
        for job in (
            await session.scalars(select(Job).where(Job.id.in_(ordered_ids)))
        ).all()
    }
    matches = {
        match.job_id: match
        for match in (
            await session.scalars(
                select(JobMatch).where(
                    JobMatch.search_run_id == run.id,
                    JobMatch.job_id.in_(ordered_ids),
                )
            )
        ).all()
    }

    # Imported here: the orchestrator imports this module for its own reasons,
    # and a module-level import would close the cycle.
    from app.services.search_orchestrator import portal_slugs_for_jobs

    slugs = await portal_slugs_for_jobs(session, job_ids=ordered_ids)

    return [
        StoredResult(
            job=jobs[job_id],
            match=matches[job_id],
            portal_slugs=slugs.get(job_id, ()),
        )
        for job_id in ordered_ids
        if job_id in jobs and job_id in matches
    ]
