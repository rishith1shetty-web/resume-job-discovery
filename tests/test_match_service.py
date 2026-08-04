"""Scoring stored jobs for a resume: persistence, isolation and eligibility.

Integration tests. Each runs inside a transaction that is always rolled back,
so nothing reaches ``resume_job_discovery`` permanently, and every record
carries the shared test marker.

The three properties worth stating plainly, because getting any of them wrong
would show a user the wrong job:

* rescoring updates one record rather than accumulating them
* one resume's scores never touch another's
* a job already applied to with a resume is not offered to that resume again
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ApplicationStatus, Seniority, SkillKind
from app.matching.eligibility import (
    ELIGIBLE_STATUSES,
    INELIGIBLE_STATUSES,
    decide,
    ineligible_job_ids,
    is_eligible,
)
from app.matching.scoring import QUALIFICATION_THRESHOLD, RejectionCode
from app.matching.version import SCORING_VERSION
from app.models.application import Application, ApplicationStatusHistory
from app.models.job import Job, JobMatch
from app.models.profile import ResumeSkill
from app.services.match_service import (
    load_match_profile,
    score_jobs_for_resume,
)
from tests.conftest import TEST_RECORD_MARKER
from tests.factories import make_job, make_profile, make_resume, make_user

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

STRONG_DESCRIPTION = """About the role
We are hiring a Financial Analyst to own forecasting and the monthly close.

Requirements:
- 3+ years of experience in financial analysis
- Advanced Excel
- SQL
- Forecasting and budgeting

Preferred qualifications:
- Power BI

Additional context about the team and the work follows here to keep this
description comfortably above the minimum length for scoring.
"""


async def analysed_resume(
    session: AsyncSession,
    *,
    target_role: str = "financial_analyst",
    domain: str = "finance",
    skills: tuple[str, ...] = (
        "excel",
        "sql",
        "forecasting",
        "budgeting",
        "financial modeling",
    ),
    years: float | None = 5.0,
    seniority: Seniority | None = Seniority.MID,
):
    """A resume with an active profile and skills, ready to be scored."""
    user = await make_user(session)
    resume = await make_resume(session, user=user)
    profile = await make_profile(
        session, resume=resume, domain=domain, target_role=target_role
    )
    profile.years_experience = years
    profile.seniority = seniority
    profile.education = ["Bachelor of Science in Finance"]

    for key in skills:
        session.add(
            ResumeSkill(
                profile_id=profile.id,
                skill_key=key,
                display=key.title(),
                kind=SkillKind.SKILL,
                occurrences=2,
                confidence=0.9,
            )
        )

    await session.flush()
    return user, resume, profile


# --- Profile loading --------------------------------------------------------


async def test_the_match_profile_comes_from_the_active_analysis(
    db_session: AsyncSession,
) -> None:
    _, resume, _ = await analysed_resume(db_session)

    profile = await load_match_profile(db_session, resume_id=resume.id)

    assert profile.target_role_key == "financial_analyst"
    assert profile.domain_key == "finance"
    assert "excel" in profile.skill_keys
    assert profile.years_experience == 5.0


async def test_an_unanalysed_resume_cannot_be_matched(
    db_session: AsyncSession,
) -> None:
    from app.core.exceptions import NotFoundError

    user = await make_user(db_session)
    resume = await make_resume(db_session, user=user)

    with pytest.raises(NotFoundError):
        await load_match_profile(db_session, resume_id=resume.id)


async def test_a_resume_with_no_target_role_is_refused_not_matched_to_everything(
    db_session: AsyncSession,
) -> None:
    from app.core.exceptions import UnprocessableError

    user = await make_user(db_session)
    resume = await make_resume(db_session, user=user)
    profile = await make_profile(db_session, resume=resume)
    profile.detected_target_role = None
    await db_session.flush()

    with pytest.raises(UnprocessableError):
        await load_match_profile(db_session, resume_id=resume.id)


# --- Scoring and persistence ------------------------------------------------


async def test_a_matching_job_is_scored_and_stored(db_session: AsyncSession) -> None:
    _, resume, _ = await analysed_resume(db_session)
    job = await make_job(
        db_session, title="Senior Financial Analyst", description=STRONG_DESCRIPTION
    )

    summary = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )

    assert summary.qualified_count == 1
    assert summary.created_count == 1

    match = await db_session.scalar(
        select(JobMatch).where(
            JobMatch.resume_id == resume.id, JobMatch.job_id == job.id
        )
    )
    assert match is not None
    assert match.qualified is True
    assert match.final_score >= QUALIFICATION_THRESHOLD
    assert match.scoring_version == SCORING_VERSION
    assert match.rejection_reasons == []
    assert match.score_details["components"]
    assert match.score_details["role"]["reason"]


async def test_a_rejected_job_is_stored_with_its_code_and_reason(
    db_session: AsyncSession,
) -> None:
    _, resume, _ = await analysed_resume(db_session)
    job = await make_job(
        db_session, title="Accountant", description=STRONG_DESCRIPTION
    )

    await score_jobs_for_resume(db_session, resume_id=resume.id, jobs=[job], now=NOW)

    match = await db_session.scalar(
        select(JobMatch).where(JobMatch.job_id == job.id)
    )
    assert match is not None
    assert match.qualified is False
    assert match.rejection_reasons[0]["code"] == (
        RejectionCode.ROLE_FAMILY_MISMATCH.value
    )
    assert match.rejection_reasons[0]["reason"]


async def test_every_component_score_is_persisted(db_session: AsyncSession) -> None:
    _, resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    await score_jobs_for_resume(db_session, resume_id=resume.id, jobs=[job], now=NOW)

    match = await db_session.scalar(select(JobMatch))
    assert match is not None
    assert match.role_score > 0
    assert match.skill_score > 0
    assert match.experience_score > 0
    assert match.final_score == match.ranking_score


# --- Idempotency ------------------------------------------------------------


async def test_rescoring_updates_one_record_rather_than_adding_another(
    db_session: AsyncSession,
) -> None:
    _, resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    first = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )
    second = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )

    assert first.created_count == 1
    assert second.created_count == 0
    assert second.updated_count == 0, "an unchanged rescore should change nothing"

    total = await db_session.scalar(select(func.count()).select_from(JobMatch))
    assert total == 1


async def test_repeated_scoring_produces_the_same_stored_score(
    db_session: AsyncSession,
) -> None:
    _, resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    first = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )
    second = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )

    assert first.scored[0].final_score == second.scored[0].final_score
    assert (
        first.scored[0].result.to_details() == second.scored[0].result.to_details()
    )


async def test_a_read_only_evaluation_stores_nothing(
    db_session: AsyncSession,
) -> None:
    _, resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    summary = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], persist=False, now=NOW
    )

    assert summary.scored[0].final_score > 0
    assert await db_session.scalar(select(func.count()).select_from(JobMatch)) == 0


# --- Resume isolation -------------------------------------------------------


async def test_two_resumes_are_matched_to_one_job_independently(
    db_session: AsyncSession,
) -> None:
    _, finance_resume, _ = await analysed_resume(db_session)
    _, data_resume, _ = await analysed_resume(
        db_session,
        target_role="data_analyst",
        domain="data",
        skills=("sql", "python", "tableau"),
    )
    job = await make_job(
        db_session, title="Financial Analyst", description=STRONG_DESCRIPTION
    )

    finance = await score_jobs_for_resume(
        db_session, resume_id=finance_resume.id, jobs=[job], now=NOW
    )
    data = await score_jobs_for_resume(
        db_session, resume_id=data_resume.id, jobs=[job], now=NOW
    )

    assert finance.scored[0].qualified is True
    assert data.scored[0].qualified is False
    assert data.scored[0].rejection_code == RejectionCode.ROLE_FAMILY_MISMATCH.value

    # Two records, one per resume, neither disturbing the other.
    assert await db_session.scalar(select(func.count()).select_from(JobMatch)) == 2


async def test_scoring_one_resume_does_not_alter_another_resumes_match(
    db_session: AsyncSession,
) -> None:
    _, first_resume, _ = await analysed_resume(db_session)
    _, second_resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    await score_jobs_for_resume(
        db_session, resume_id=first_resume.id, jobs=[job], now=NOW
    )
    original = await db_session.scalar(
        select(JobMatch).where(JobMatch.resume_id == first_resume.id)
    )
    assert original is not None
    before = (original.final_score, original.matched_at)

    await score_jobs_for_resume(
        db_session, resume_id=second_resume.id, jobs=[job], now=NOW
    )
    await db_session.refresh(original)

    assert (original.final_score, original.matched_at) == before


# --- Profile correction -----------------------------------------------------


async def test_correcting_the_target_role_changes_the_next_rescore(
    db_session: AsyncSession,
) -> None:
    """A correction must take effect, without erasing the detection."""
    _, resume, profile = await analysed_resume(
        db_session, target_role="data_analyst", domain="data"
    )
    job = await make_job(
        db_session, title="Financial Analyst", description=STRONG_DESCRIPTION
    )

    before = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )
    assert before.scored[0].qualified is False

    profile.confirmed_target_role = "financial_analyst"
    profile.confirmed_domain = "finance"
    profile.confirmed_at = NOW
    await db_session.flush()

    after = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )

    assert after.scored[0].qualified is True
    assert after.updated_count == 1
    # The detection itself is untouched.
    assert profile.detected_target_role == "data_analyst"
    # And still one record for the pair.
    assert await db_session.scalar(select(func.count()).select_from(JobMatch)) == 1


# --- Deduplicated jobs ------------------------------------------------------


async def test_a_job_carried_by_two_portals_produces_one_match(
    db_session: AsyncSession,
) -> None:
    """Matching keys on the canonical job, after deduplication.

    Two portal sources are two ``job_sources`` rows on one ``jobs`` row, so
    there is one thing to score and one verdict to store.
    """
    from tests.factories import make_job_source, make_portal

    _, resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)
    alpha = await make_portal(db_session, slug="alpha")
    beta = await make_portal(db_session, slug="beta")
    await make_job_source(db_session, job=job, portal=alpha, external_id="a-1")
    await make_job_source(db_session, job=job, portal=beta, external_id="b-1")

    summary = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )

    assert len(summary.scored) == 1
    assert await db_session.scalar(select(func.count()).select_from(JobMatch)) == 1


# --- Applied-job eligibility ------------------------------------------------


@pytest.mark.parametrize("status", sorted(ELIGIBLE_STATUSES))
def test_uncommitted_statuses_stay_eligible(status: ApplicationStatus) -> None:
    decision = decide(resume_id=uuid.uuid4(), job_id=uuid.uuid4(), status=status)

    assert decision.is_eligible is True


@pytest.mark.parametrize("status", sorted(INELIGIBLE_STATUSES))
def test_submitted_statuses_become_ineligible(status: ApplicationStatus) -> None:
    decision = decide(resume_id=uuid.uuid4(), job_id=uuid.uuid4(), status=status)

    assert decision.is_eligible is False
    assert "history" in decision.reason


def test_every_application_status_is_classified_exactly_once() -> None:
    # A status in neither set would silently default to eligible.
    assert ELIGIBLE_STATUSES | INELIGIBLE_STATUSES == set(ApplicationStatus)
    assert not (ELIGIBLE_STATUSES & INELIGIBLE_STATUSES)


def test_a_job_with_no_application_is_eligible() -> None:
    decision = decide(resume_id=uuid.uuid4(), job_id=uuid.uuid4(), status=None)

    assert decision.is_eligible is True


async def test_an_applied_job_is_not_offered_to_that_resume_again(
    db_session: AsyncSession,
) -> None:
    user, resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job.id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    summary = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )
    scored = summary.scored[0]

    # It still scores well - it is a good match - but it is not presentable.
    assert scored.qualified is True
    assert scored.eligibility.is_eligible is False
    assert scored.is_presentable is False
    assert summary.presentable_count == 0
    assert summary.excluded_as_applied == 1


async def test_the_same_job_stays_eligible_for_a_different_resume(
    db_session: AsyncSession,
) -> None:
    user, applied_resume, _ = await analysed_resume(db_session)
    other_resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=other_resume)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=applied_resume.id,
            job_id=job.id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    blocked = await is_eligible(
        db_session, resume_id=applied_resume.id, job_id=job.id
    )
    open_for_other = await is_eligible(
        db_session, resume_id=other_resume.id, job_id=job.id
    )

    assert blocked.is_eligible is False
    assert open_for_other.is_eligible is True


@pytest.mark.parametrize(
    "status",
    [ApplicationStatus.NOT_APPLIED, ApplicationStatus.SAVED, ApplicationStatus.SKIPPED],
)
async def test_saved_and_not_applied_jobs_remain_presentable(
    db_session: AsyncSession, status: ApplicationStatus
) -> None:
    user, resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job.id,
            status=status,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    summary = await score_jobs_for_resume(
        db_session, resume_id=resume.id, jobs=[job], now=NOW
    )

    assert summary.scored[0].is_presentable is True


async def test_the_excluded_set_is_scoped_to_one_resume(
    db_session: AsyncSession,
) -> None:
    user, applied_resume, _ = await analysed_resume(db_session)
    other_resume = await make_resume(db_session, user=user)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=applied_resume.id,
            job_id=job.id,
            status=ApplicationStatus.INTERVIEW,
            applied_at=NOW,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    assert await ineligible_job_ids(db_session, resume_id=applied_resume.id) == {
        job.id
    }
    assert await ineligible_job_ids(db_session, resume_id=other_resume.id) == frozenset()


async def test_scoring_never_writes_application_state(
    db_session: AsyncSession,
) -> None:
    """Matching reads eligibility; it never changes it.

    An applied job keeps its match record too - it stays in history, and the
    record is not deleted when the job stops being presentable.
    """
    user, resume, _ = await analysed_resume(db_session)
    job = await make_job(db_session, description=STRONG_DESCRIPTION)

    application = Application(
        user_id=user.id,
        resume_id=resume.id,
        job_id=job.id,
        status=ApplicationStatus.APPLIED,
        applied_at=NOW,
        notes=TEST_RECORD_MARKER,
    )
    db_session.add(application)
    await db_session.flush()

    await score_jobs_for_resume(db_session, resume_id=resume.id, jobs=[job], now=NOW)
    await db_session.refresh(application)

    assert application.status is ApplicationStatus.APPLIED
    assert application.applied_at == NOW
    assert await db_session.scalar(select(func.count()).select_from(Application)) == 1
    assert (
        await db_session.scalar(
            select(func.count()).select_from(ApplicationStatusHistory)
        )
        == 0
    )
    # The match record survives; it is history, not a result.
    assert await db_session.scalar(select(func.count()).select_from(JobMatch)) == 1


# --- Job selection ----------------------------------------------------------


async def test_expired_jobs_are_not_offered_for_scoring(
    db_session: AsyncSession,
) -> None:
    from app.services.match_service import load_jobs

    _, _, _ = await analysed_resume(db_session)
    live = await make_job(db_session, description=STRONG_DESCRIPTION)
    await make_job(
        db_session, description=STRONG_DESCRIPTION, expired_at=NOW, title="Old Analyst"
    )

    jobs = await load_jobs(db_session, job_ids=None, limit=10)

    assert [job.id for job in jobs] == [live.id]


async def test_scoring_refuses_an_oversized_batch(db_session: AsyncSession) -> None:
    from app.core.exceptions import UnprocessableError
    from app.services.match_service import MAX_JOBS_PER_CALL

    _, resume, _ = await analysed_resume(db_session)
    jobs = [Job(canonical_key=f"k{i}", title="Financial Analyst") for i in range(MAX_JOBS_PER_CALL + 1)]

    with pytest.raises(UnprocessableError):
        await score_jobs_for_resume(db_session, resume_id=resume.id, jobs=jobs)
