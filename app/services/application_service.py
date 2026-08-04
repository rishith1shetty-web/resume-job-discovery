"""Manual application status and its history.

The rules this module exists to guarantee:

* An application is keyed on (user, resume, canonical job). Rediscovering the
  same vacancy through a second portal resolves to the same row, so a job
  already marked APPLIED never comes back as a new unapplied opportunity
  (CLAUDE.md section 9, docs/DECISIONS.md D-003).
* Every real transition writes one immutable history row.
* Re-sending the current status changes nothing and writes no history.
* Reaching a submitted status stamps ``applied_at`` once; later changes keep
  the original timestamp.
* A job expiring never touches any of this. Expiry lives on ``jobs.expired_at``
  and is not an application status.
* Recording a status never rewrites search history. This module writes to
  ``applications`` and ``application_status_history`` and to nothing else, so a
  match score, a run summary or a portal result cannot move because the user
  changed a status afterwards.

# Where ``status_changed_at`` comes from

There is no ``status_changed_at`` column, and Phase 8 did not add one. Every
transition already writes an ``application_status_history`` row, so the moment
the status last changed *is* the newest history row's ``changed_at``. Deriving
it keeps one fact in one place: a stored column could drift from the history it
claims to summarise, and there is no way for that to happen here.

``get_or_create_application`` writes an opening row, so the value is always
defined for an application that exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ApplicationStatus
from app.core.exceptions import ErrorDetail, NotFoundError
from app.core.logging import get_logger
from app.models.application import (
    APPLIED_STATUSES,
    Application,
    ApplicationStatusHistory,
)
from app.models.job import Job
from app.models.resume import Resume

logger = get_logger(__name__)


#: When this application's status last actually changed. Correlated so it can
#: be selected alongside the application row in one query rather than one
#: lookup per result.
_STATUS_CHANGED_AT = (
    select(func.max(ApplicationStatusHistory.changed_at))
    .where(ApplicationStatusHistory.application_id == Application.id)
    .correlate(Application)
    .scalar_subquery()
)


@dataclass(slots=True)
class ApplicationView:
    """An application with the derived values the dashboard needs.

    ``status_changed_at`` is read from the history rather than stored, so it
    can never disagree with the transitions it summarises. It falls back to
    ``created_at`` only in the impossible case of an application with no
    history at all.
    """

    application: Application
    job: Job | None = None
    status_changed_at: datetime | None = None

    @property
    def effective_status_changed_at(self) -> datetime:
        return self.status_changed_at or self.application.created_at

    @property
    def has_notes(self) -> bool:
        return bool((self.application.notes or "").strip())


async def _require_job(session: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await session.get(Job, job_id)
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
    return job


async def _require_resume(
    session: AsyncSession, *, resume_id: uuid.UUID, user_id: uuid.UUID
) -> Resume:
    resume = await session.scalar(
        select(Resume).where(Resume.id == resume_id, Resume.user_id == user_id)
    )
    if resume is None:
        raise NotFoundError(
            message="Resume not found.",
            details=[
                ErrorDetail(
                    code="resume_not_found",
                    field="query.resume_id",
                    message=f"No resume with id {resume_id}.",
                )
            ],
        )
    return resume


async def require_resume_and_job(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    job_id: uuid.UUID,
) -> tuple[Resume, Job]:
    """Check both ids exist and belong to this user, or raise.

    Used by reads as well as writes: answering "not applied" for a resume or
    job that does not exist would hide a client bug behind a plausible answer.
    """
    resume = await _require_resume(session, resume_id=resume_id, user_id=user_id)
    job = await _require_job(session, job_id)
    return resume, job


async def get_application(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    job_id: uuid.UUID,
) -> Application | None:
    return await session.scalar(
        select(Application).where(
            Application.user_id == user_id,
            Application.resume_id == resume_id,
            Application.job_id == job_id,
        )
    )


async def load_application_view(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    job_id: uuid.UUID,
) -> ApplicationView | None:
    """One application with its job and derived timestamp, or None.

    Returning None rather than raising is deliberate: "this resume has not
    recorded anything for this job yet" is a normal state, not an error. The
    caller renders it as ``not_applied``.
    """
    row = (
        await session.execute(
            select(Application, Job, _STATUS_CHANGED_AT)
            .join(Job, Job.id == Application.job_id)
            .where(
                Application.user_id == user_id,
                Application.resume_id == resume_id,
                Application.job_id == job_id,
            )
        )
    ).first()

    if row is None:
        return None

    application, job, status_changed_at = row
    return ApplicationView(
        application=application, job=job, status_changed_at=status_changed_at
    )


async def get_or_create_application(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    job_id: uuid.UUID,
) -> Application:
    """Return the existing application, or start one at NOT_APPLIED.

    This is the function rediscovery must go through. Because the key is the
    canonical job, a second portal surfacing the same vacancy finds the row
    that already exists rather than creating a fresh, unapplied one.
    """
    existing = await get_application(
        session, user_id=user_id, resume_id=resume_id, job_id=job_id
    )
    if existing is not None:
        return existing

    application = Application(
        user_id=user_id,
        resume_id=resume_id,
        job_id=job_id,
        status=ApplicationStatus.NOT_APPLIED,
    )
    session.add(application)
    await session.flush()

    # The opening history row: no previous status, because there was no
    # application before this.
    session.add(
        ApplicationStatusHistory(
            application_id=application.id,
            old_status=None,
            new_status=ApplicationStatus.NOT_APPLIED,
        )
    )
    await session.flush()
    return application


async def set_status(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    job_id: uuid.UUID,
    status: ApplicationStatus,
    notes: str | None = None,
) -> tuple[Application, bool]:
    """Record the status the user chose.

    Returns ``(application, changed)``. ``changed`` is False when the status
    was already the requested one, in which case no history row is written.
    """
    await _require_resume(session, resume_id=resume_id, user_id=user_id)
    await _require_job(session, job_id)

    application = await get_or_create_application(
        session, user_id=user_id, resume_id=resume_id, job_id=job_id
    )

    previous = application.status
    changed = previous != status

    if notes is not None:
        application.notes = notes

    if not changed:
        # A repeated identical update is a no-op, not a duplicate history row.
        logger.info(
            "application_status_unchanged",
            application_id=str(application.id),
            status=status.value,
        )
        await session.flush()
        return application, False

    application.status = status

    # Stamped once. A later move to INTERVIEW or OFFER keeps the original
    # submission time rather than resetting it.
    if status in APPLIED_STATUSES and application.applied_at is None:
        application.applied_at = datetime.now(UTC)

    session.add(
        ApplicationStatusHistory(
            application_id=application.id,
            old_status=previous,
            new_status=status,
            note=notes,
        )
    )
    await session.flush()

    logger.info(
        "application_status_changed",
        application_id=str(application.id),
        old_status=previous.value,
        new_status=status.value,
    )
    return application, True


async def list_applications(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID | None = None,
    status: ApplicationStatus | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[ApplicationView], int]:
    """Page through applications, newest first.

    Filtering by resume is what keeps two resumes' histories apart; without it
    the caller sees only their own applications, never another user's.
    """
    filters = [Application.user_id == user_id]
    if resume_id is not None:
        filters.append(Application.resume_id == resume_id)
    if status is not None:
        filters.append(Application.status == status)

    total = await session.scalar(
        select(func.count()).select_from(Application).where(*filters)
    )

    result = await session.execute(
        select(Application, Job, _STATUS_CHANGED_AT)
        .join(Job, Job.id == Application.job_id)
        .where(*filters)
        # `id` only breaks ties: two applications updated inside one
        # transaction share a timestamp, and without it their order would vary
        # between identical requests.
        .order_by(Application.updated_at.desc(), Application.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return [
        ApplicationView(
            application=application, job=job, status_changed_at=changed_at
        )
        for application, job, changed_at in result.all()
    ], int(total or 0)


async def list_status_history(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[tuple[ApplicationStatusHistory, Application, Job]], int]:
    """Every transition for one user, newest first.

    Joined to the job so a history row is readable on its own instead of
    needing a lookup per line. Filtering by ``job_id`` narrows it to one
    application's own timeline; the canonical job is the key, so a vacancy
    found through several portals still has exactly one timeline.
    """
    filters = [Application.user_id == user_id]
    if resume_id is not None:
        filters.append(Application.resume_id == resume_id)
    if job_id is not None:
        filters.append(Application.job_id == job_id)

    total = await session.scalar(
        select(func.count())
        .select_from(ApplicationStatusHistory)
        .join(
            Application,
            Application.id == ApplicationStatusHistory.application_id,
        )
        .where(*filters)
    )

    result = await session.execute(
        select(ApplicationStatusHistory, Application, Job)
        .join(
            Application,
            Application.id == ApplicationStatusHistory.application_id,
        )
        .join(Job, Job.id == Application.job_id)
        .where(*filters)
        # `id` only breaks ties: two rows can share a timestamp when the clock
        # has coarser resolution than the gap between writes. Without it the
        # order of such rows would vary between identical requests.
        .order_by(
            ApplicationStatusHistory.changed_at.desc(),
            ApplicationStatusHistory.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return [tuple(row) for row in result.all()], int(total or 0)  # type: ignore[misc]
