"""Scoring canonical jobs for one resume, and storing the verdicts.

The only place Phase 6's rules meet the database. Everything it decides comes
from ``app.matching``; this module loads the inputs, applies the result and
writes it.

Guarantees it is built to keep:

* **One current match per (resume, canonical job).** Rescoring updates that
  row rather than appending another, so a job cannot appear twice with two
  different scores.
* **Resumes never interfere.** Every query is filtered by ``resume_id``, and
  scoring one resume touches no other resume's rows (CLAUDE.md section 11).
* **Corrections take effect.** The profile is read through ``effective_*``, so
  correcting the detected role and rescoring changes the outcome.
* **Application state is read, never written.** Eligibility is consulted;
  nothing here creates, updates or deletes an application.

Not in scope: quotas, portal fairness, freshness windows, result filling and
the Find Matches orchestration itself. Those are Phase 7, and this is what
they will call.

# Note on the unique constraint

``job_matches`` carries ``UNIQUE (resume_id, job_id, search_run_id)``. In
PostgreSQL two NULLs are distinct, so that constraint does *not* prevent two
run-less rows for one pair. This module therefore reads the existing row and
updates it, which is sufficient while scoring is synchronous and single-user.
When Phase 7 introduces background workers, a partial unique index on
``(resume_id, job_id) WHERE search_run_id IS NULL`` should be added so the
database enforces it too. That needs a migration and is not created here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import ErrorDetail, NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.matching.eligibility import EligibilityDecision, decide, ineligible_job_ids
from app.matching.profile import ResumeMatchProfile, build_match_profile
from app.matching.requirements import JobRequirements, extract_requirements
from app.matching.scoring import MatchResult, score_match
from app.matching.version import SCORING_VERSION
from app.models.application import Application
from app.models.job import Job, JobMatch
from app.models.profile import ResumeProfile

logger = get_logger(__name__)

#: A safety bound on how many jobs one call will score. Phase 7 owns batching.
MAX_JOBS_PER_CALL = 200


@dataclass(slots=True)
class ScoredJob:
    """One job scored for one resume, with what happened to its record."""

    job_id: uuid.UUID
    title: str
    company: str | None
    final_score: float
    qualified: bool
    rejection_code: str | None
    rejection_reason: str | None
    result: MatchResult
    eligibility: EligibilityDecision
    #: True when a new ``job_matches`` row was inserted.
    created: bool = False
    #: True when an existing row's score changed.
    updated: bool = False

    @property
    def is_presentable(self) -> bool:
        """Qualified *and* not already applied to with this resume."""
        return self.qualified and self.eligibility.is_eligible


@dataclass(slots=True)
class ScoringSummary:
    """Outcome of scoring a set of jobs for one resume."""

    resume_id: uuid.UUID
    scoring_version: str = SCORING_VERSION
    scored: list[ScoredJob] = field(default_factory=list)

    @property
    def qualified_count(self) -> int:
        return sum(1 for item in self.scored if item.qualified)

    @property
    def presentable_count(self) -> int:
        return sum(1 for item in self.scored if item.is_presentable)

    @property
    def excluded_as_applied(self) -> int:
        return sum(1 for item in self.scored if not item.eligibility.is_eligible)

    @property
    def created_count(self) -> int:
        return sum(1 for item in self.scored if item.created)

    @property
    def updated_count(self) -> int:
        return sum(1 for item in self.scored if item.updated)


async def load_match_profile(
    session: AsyncSession, *, resume_id: uuid.UUID
) -> ResumeMatchProfile:
    """Build the candidate side from the resume's active analysis.

    Raises rather than scoring against a resume that has not been analysed:
    a match with no target role would let every job through the gate, which is
    the failure CLAUDE.md section 3 exists to prevent.
    """
    profile = await session.scalar(
        select(ResumeProfile)
        .where(
            ResumeProfile.resume_id == resume_id,
            ResumeProfile.is_active.is_(True),
        )
        .options(selectinload(ResumeProfile.skills))
    )

    if profile is None:
        raise NotFoundError(
            message="This resume has not been analysed yet.",
            details=[
                ErrorDetail(
                    code="resume_profile_not_found",
                    field="query.resume_id",
                    message=(
                        "Run POST /resumes/{id}/analyze before matching jobs "
                        "against it."
                    ),
                )
            ],
        )

    match_profile = build_match_profile(profile, list(profile.skills))

    if not match_profile.is_scoreable:
        raise UnprocessableError(
            message="This resume has no detected target role.",
            details=[
                ErrorDetail(
                    code="no_target_role",
                    field="query.resume_id",
                    message=(
                        "Matching needs exactly one target role. Set it with "
                        "PATCH /resumes/{id}/profile, then try again."
                    ),
                )
            ],
        )

    return match_profile


def job_requirements(job: Job) -> JobRequirements:
    """Read one canonical job into the facts scoring may use."""
    return extract_requirements(
        title=job.title, description=job.description, location=job.location
    )


async def score_jobs_for_resume(
    session: AsyncSession,
    *,
    resume_id: uuid.UUID,
    jobs: list[Job],
    persist: bool = True,
    search_run_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> ScoringSummary:
    """Score every given job for one resume.

    ``persist=False`` makes this a read-only evaluation: the same scores and
    the same explanations, with nothing written. That is what the preview
    endpoint uses.
    """
    if len(jobs) > MAX_JOBS_PER_CALL:
        raise UnprocessableError(
            message="Too many jobs in one scoring call.",
            details=[
                ErrorDetail(
                    code="too_many_jobs",
                    field="body.job_ids",
                    message=f"At most {MAX_JOBS_PER_CALL} jobs can be scored at once.",
                )
            ],
        )

    profile = await load_match_profile(session, resume_id=resume_id)
    excluded = await ineligible_job_ids(session, resume_id=resume_id)
    statuses = await _application_statuses(session, resume_id=resume_id)
    moment = now or datetime.now(UTC)

    summary = ScoringSummary(resume_id=resume_id)

    for job in jobs:
        result = score_match(profile, job_requirements(job))
        eligibility = decide(
            resume_id=resume_id, job_id=job.id, status=statuses.get(job.id)
        )

        scored = ScoredJob(
            job_id=job.id,
            title=job.title,
            company=job.company,
            final_score=result.final_score,
            qualified=result.qualified,
            rejection_code=(
                result.rejection_code.value if result.rejection_code else None
            ),
            rejection_reason=result.rejection_reason,
            result=result,
            eligibility=eligibility,
        )

        if persist:
            created, updated = await _upsert_match(
                session,
                resume_id=resume_id,
                job=job,
                result=result,
                search_run_id=search_run_id,
                moment=moment,
            )
            scored.created = created
            scored.updated = updated

        summary.scored.append(scored)

    logger.info(
        "resume_jobs_scored",
        resume_id=str(resume_id),
        jobs=len(jobs),
        qualified=summary.qualified_count,
        presentable=summary.presentable_count,
        excluded_as_applied=len(excluded),
        persisted=persist,
        scoring_version=SCORING_VERSION,
    )

    return summary


async def _application_statuses(
    session: AsyncSession, *, resume_id: uuid.UUID
) -> dict[uuid.UUID, object]:
    """Current application status per job, for this resume only."""
    rows = await session.execute(
        select(Application.job_id, Application.status).where(
            Application.resume_id == resume_id
        )
    )
    return {job_id: status for job_id, status in rows}


async def _upsert_match(
    session: AsyncSession,
    *,
    resume_id: uuid.UUID,
    job: Job,
    result: MatchResult,
    search_run_id: uuid.UUID | None,
    moment: datetime,
) -> tuple[bool, bool]:
    """Write the verdict, updating the existing row for this pair.

    Returns ``(created, updated)``. Rescoring an unchanged input reports
    neither, so a caller can tell a real change from a no-op.
    """
    existing = await session.scalar(
        select(JobMatch).where(
            JobMatch.resume_id == resume_id,
            JobMatch.job_id == job.id,
            JobMatch.search_run_id.is_(None)
            if search_run_id is None
            else JobMatch.search_run_id == search_run_id,
        )
    )

    values = _match_values(result)

    if existing is None:
        session.add(
            JobMatch(
                resume_id=resume_id,
                job_id=job.id,
                search_run_id=search_run_id,
                matched_at=moment,
                **values,
            )
        )
        await session.flush()
        return True, False

    changed = any(
        getattr(existing, name) != value for name, value in values.items()
    )
    if not changed:
        return False, False

    for name, value in values.items():
        setattr(existing, name, value)
    existing.matched_at = moment
    await session.flush()

    return False, True


def _match_values(result: MatchResult) -> dict[str, object]:
    """Map a scoring result onto the ``job_matches`` columns.

    ``ranking_score`` is left equal to the final score here. Freshness and
    source quality join the ranking in Phase 7, where a search run has the
    context to weigh them (CLAUDE.md section 8).
    """
    components = result.component_scores

    return {
        "final_score": result.final_score,
        "role_score": round(components.get("role", 0.0), 4),
        "skill_score": round(
            components.get("required_skills", 0.0)
            + components.get("preferred_skills", 0.0),
            4,
        ),
        "experience_score": round(components.get("experience", 0.0), 4),
        "responsibility_score": round(components.get("supporting", 0.0), 4),
        "domain_score": round(components.get("domain_education", 0.0), 4),
        "education_score": round(components.get("domain_education", 0.0), 4),
        "freshness_score": 0.0,
        "ranking_score": result.final_score,
        "qualified": result.qualified,
        "rejection_reasons": (
            [
                {
                    "code": result.rejection_code.value,
                    "reason": result.rejection_reason,
                }
            ]
            if result.rejection_code
            else []
        ),
        "score_details": result.to_details(),
        "scoring_version": result.scoring_version,
    }


async def load_jobs(
    session: AsyncSession, *, job_ids: list[uuid.UUID] | None, limit: int
) -> list[Job]:
    """Load canonical jobs to score, newest first when no ids are given."""
    statement = select(Job).where(Job.expired_at.is_(None))

    if job_ids:
        statement = statement.where(Job.id.in_(job_ids))

    statement = statement.order_by(
        Job.posted_at.desc().nullslast(), Job.id
    ).limit(limit)

    return list((await session.scalars(statement)).all())
