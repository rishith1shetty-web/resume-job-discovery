"""Which canonical jobs a resume may still be shown.

The permanent rule (Phase 6 brief, CLAUDE.md sections 9 and 11):

    A canonical job the user has **applied to for a given resume** is excluded
    from future Find Matches results **for that same resume**. It stays
    visible in application history, its match record is never deleted, and the
    same job remains eligible for a different resume.

Everything hangs on the pairing being (resume, canonical job) - resolved
*after* deduplication. A job rediscovered through a second portal is the same
canonical row, so it cannot come back as a new opportunity under another
portal's listing.

``NOT_APPLIED``, ``SAVED`` and ``SKIPPED`` stay eligible: the user has not
committed to any of them.

# How Phase 7 uses this

Phase 7's search orchestration calls ``ineligible_job_ids`` once per run,
after deduplication has resolved every collected listing to a canonical job
and before quotas are filled:

    excluded = await ineligible_job_ids(session, resume_id=resume.id)
    candidates = [job for job in canonical_jobs if job.id not in excluded]

Excluding before quota filling matters: a quota counts *presentable* jobs, so
filtering afterwards would silently under-fill a portal's share. The set is
small (one row per application), so one query per run is enough.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ApplicationStatus
from app.models.application import APPLIED_STATUSES, Application

#: Statuses that keep a job in the results. Everything the user has not
#: actually submitted stays visible.
ELIGIBLE_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        ApplicationStatus.NOT_APPLIED,
        ApplicationStatus.SAVED,
        ApplicationStatus.SKIPPED,
    }
)

#: Statuses that remove a job from future results for that resume. Identical
#: to the set that stamps ``applied_at``: reaching an interview or an outcome
#: means the application was submitted.
INELIGIBLE_STATUSES: frozenset[ApplicationStatus] = APPLIED_STATUSES


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Whether one job may be shown for one resume, and why."""

    job_id: uuid.UUID
    resume_id: uuid.UUID
    is_eligible: bool
    status: ApplicationStatus | None
    reason: str


def decide(
    *,
    resume_id: uuid.UUID,
    job_id: uuid.UUID,
    status: ApplicationStatus | None,
) -> EligibilityDecision:
    """Pure eligibility rule, with no database access.

    Kept separate so the rule can be tested exhaustively over every status
    without a session, and so Phase 7 can apply it to values it already holds.
    """
    if status is None:
        return EligibilityDecision(
            job_id=job_id,
            resume_id=resume_id,
            is_eligible=True,
            status=None,
            reason="No application recorded for this resume and job.",
        )

    if status in INELIGIBLE_STATUSES:
        return EligibilityDecision(
            job_id=job_id,
            resume_id=resume_id,
            is_eligible=False,
            status=status,
            reason=(
                f"Already applied for this resume (status {status.value}). It "
                "stays in application history and is not offered again."
            ),
        )

    return EligibilityDecision(
        job_id=job_id,
        resume_id=resume_id,
        is_eligible=True,
        status=status,
        reason=f"Status {status.value} does not remove the job from results.",
    )


async def ineligible_job_ids(
    session: AsyncSession, *, resume_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Canonical job ids this resume must not be offered again.

    Scoped to one resume by design: the same vacancy stays available to every
    other resume the user owns, because an application belongs to the resume
    it was submitted with (CLAUDE.md section 11).
    """
    statement = select(Application.job_id).where(
        Application.resume_id == resume_id,
        Application.status.in_(tuple(INELIGIBLE_STATUSES)),
    )

    return frozenset((await session.scalars(statement)).all())


async def is_eligible(
    session: AsyncSession, *, resume_id: uuid.UUID, job_id: uuid.UUID
) -> EligibilityDecision:
    """Eligibility for one (resume, job) pair, reading the stored status."""
    status = await session.scalar(
        select(Application.status).where(
            Application.resume_id == resume_id,
            Application.job_id == job_id,
        )
    )

    return decide(resume_id=resume_id, job_id=job_id, status=status)
