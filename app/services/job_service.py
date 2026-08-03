"""Reading canonical jobs for one resume.

Every query here is scoped to a resume. A job's score and its application
status both belong to a (resume, job) pair, so listing jobs without a resume
would mix two resumes' results - exactly what CLAUDE.md section 11 forbids.

Phase 3 reads only. Jobs are written by the collection and normalisation
pipeline in Phases 4 and 5.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.core.constants import ApplicationStatus, SortOption
from app.core.exceptions import ErrorDetail, NotFoundError
from app.models.application import Application
from app.models.job import Job, JobMatch, JobSource
from app.models.portal import Portal
from app.schemas.job import JobDetail, JobListItem, JobMatchOut, JobSourceOut


def _latest_match_alias(
    *, resume_id: uuid.UUID, search_run_id: uuid.UUID | None
) -> Any:
    """One match row per job: the newest for this resume.

    Without this a job scored in three runs would appear three times in the
    list and inflate the total. DISTINCT ON keeps the newest verdict per job,
    or the verdict from one specific run when the caller asked for that.
    """
    statement = select(JobMatch).where(JobMatch.resume_id == resume_id)
    if search_run_id is not None:
        statement = statement.where(JobMatch.search_run_id == search_run_id)

    newest = (
        # `id` only breaks ties: two rescores can share a timestamp when the
        # clock is coarser than the gap between them, and DISTINCT ON would
        # then pick arbitrarily.
        statement.order_by(
            JobMatch.job_id, JobMatch.matched_at.desc(), JobMatch.id.desc()
        )
        .distinct(JobMatch.job_id)
        .subquery()
    )
    return aliased(JobMatch, newest)


def _build_query(
    *,
    resume_id: uuid.UUID,
    user_id: uuid.UUID,
    search_run_id: uuid.UUID | None,
    portal_slug: str | None,
    status: ApplicationStatus | None,
    include_expired: bool,
) -> tuple[Select[Any], Any]:
    """Jobs joined to this resume's match and application state.

    Outer joins throughout: a job with no score yet, or one the user has not
    touched, must still be listable.
    """
    match_alias = _latest_match_alias(
        resume_id=resume_id, search_run_id=search_run_id
    )

    statement = (
        select(Job)
        .outerjoin(match_alias, match_alias.job_id == Job.id)
        .outerjoin(
            Application,
            (Application.job_id == Job.id)
            & (Application.resume_id == resume_id)
            & (Application.user_id == user_id),
        )
    )

    if search_run_id is not None:
        # Restrict to jobs that run actually produced.
        statement = statement.where(match_alias.id.is_not(None))
    if status is not None:
        if status is ApplicationStatus.NOT_APPLIED:
            # No application row means not applied; treat both the same.
            statement = statement.where(
                (Application.status == status) | (Application.id.is_(None))
            )
        else:
            statement = statement.where(Application.status == status)
    if portal_slug is not None:
        statement = statement.where(
            Job.id.in_(
                select(JobSource.job_id)
                .join(Portal, Portal.id == JobSource.portal_id)
                .where(Portal.slug == portal_slug)
            )
        )
    if not include_expired:
        statement = statement.where(Job.expired_at.is_(None))

    return statement, match_alias


def _apply_sort(
    statement: Select[Any], sort: SortOption, match_alias: Any
) -> Select[Any]:
    """Order results.

    BEST_OVERALL uses the stored ranking score, which already blends match and
    freshness. Freshness only breaks ties inside that blend - a strong older
    match still outranks a weak new one (CLAUDE.md section 8).
    """
    match sort:
        case SortOption.HIGHEST_MATCH:
            return statement.order_by(
                match_alias.final_score.desc().nullslast(),
                Job.posted_at.desc().nullslast(),
            )
        case SortOption.NEWEST_FIRST:
            return statement.order_by(
                Job.posted_at.desc().nullslast(), Job.first_discovered_at.desc()
            )
        case SortOption.COMPANY:
            return statement.order_by(Job.company.asc().nullslast(), Job.title.asc())
        case SortOption.STATUS:
            return statement.order_by(
                Application.status.asc().nullsfirst(), Job.title.asc()
            )
        case SortOption.PORTAL:
            # Portal belongs to the sources, not the job. The canonical key
            # gives a stable order; the client groups by the `portals` field it
            # already receives.
            return statement.order_by(Job.canonical_key.asc())
        case _:
            return statement.order_by(
                match_alias.ranking_score.desc().nullslast(),
                Job.posted_at.desc().nullslast(),
            )


def _preferred_source(job: Job) -> JobSource | None:
    """The source that supplied the job's canonical URL, if one is marked."""
    for source in job.sources:
        if source.is_preferred:
            return source
    return job.sources[0] if job.sources else None


def _to_list_item(
    job: Job, match: JobMatch | None, application: Application | None
) -> JobListItem:
    source = _preferred_source(job)

    return JobListItem(
        id=job.id,
        title=job.title,
        company=job.company,
        location=job.location,
        work_mode=job.work_mode,
        employment_type=job.employment_type,
        portals=[item.portal.slug for item in job.sources if item.portal is not None],
        posted_at=job.posted_at,
        first_discovered_at=job.first_discovered_at,
        last_seen_at=job.last_seen_at,
        expired_at=job.expired_at,
        is_expired=job.is_expired,
        # Falls back to the source URL so a row is never unusable: the user has
        # to be able to open something and apply manually.
        application_url=(
            job.preferred_application_url
            or (source.application_url or source.source_url if source else None)
        ),
        url_kind=job.preferred_url_kind or (source.url_kind if source else None),
        match=JobMatchOut.model_validate(match) if match is not None else None,
        application_status=(
            application.status if application else ApplicationStatus.NOT_APPLIED
        ),
        applied_at=application.applied_at if application else None,
    )


async def list_jobs(
    session: AsyncSession,
    *,
    resume_id: uuid.UUID,
    user_id: uuid.UUID,
    search_run_id: uuid.UUID | None = None,
    portal_slug: str | None = None,
    status: ApplicationStatus | None = None,
    sort: SortOption = SortOption.BEST_OVERALL,
    include_expired: bool = False,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[JobListItem], int]:
    statement, match_alias = _build_query(
        resume_id=resume_id,
        user_id=user_id,
        search_run_id=search_run_id,
        portal_slug=portal_slug,
        status=status,
        include_expired=include_expired,
    )

    # The statement selects only Job, so the subquery has no duplicate labels.
    total = await session.scalar(
        select(func.count()).select_from(statement.subquery())
    )

    rows = await session.execute(
        _apply_sort(statement, sort, match_alias)
        .add_columns(match_alias, Application)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .options(selectinload(Job.sources).selectinload(JobSource.portal))
    )

    return [
        _to_list_item(job, match, application)
        for job, match, application in rows.all()
    ], int(total or 0)


async def get_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    resume_id: uuid.UUID,
    user_id: uuid.UUID,
) -> JobDetail:
    job = await session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .options(selectinload(Job.sources).selectinload(JobSource.portal))
    )
    if job is None:
        raise NotFoundError(
            message="Job not found.",
            details=[
                ErrorDetail(
                    code="job_not_found",
                    field="path.job_id",
                    message=f"No job with id {job_id}.",
                )
            ],
        )

    match = await session.scalar(
        select(JobMatch)
        .where(JobMatch.job_id == job.id, JobMatch.resume_id == resume_id)
        .order_by(JobMatch.matched_at.desc())
        .limit(1)
    )
    application = await session.scalar(
        select(Application).where(
            Application.job_id == job.id,
            Application.resume_id == resume_id,
            Application.user_id == user_id,
        )
    )

    base = _to_list_item(job, match, application)

    return JobDetail(
        **base.model_dump(),
        description=job.description,
        normalized_title=job.normalized_title,
        canonical_key=job.canonical_key,
        last_verified_at=job.last_verified_at,
        sources=[
            JobSourceOut(
                portal_id=source.portal_id,
                portal_slug=source.portal.slug if source.portal else None,
                portal_display_name=(
                    source.portal.display_name if source.portal else None
                ),
                external_id=source.external_id,
                source_url=source.source_url,
                application_url=source.application_url,
                url_kind=source.url_kind,
                posted_at=source.posted_at,
                is_preferred=source.is_preferred,
            )
            for source in job.sources
        ],
    )
