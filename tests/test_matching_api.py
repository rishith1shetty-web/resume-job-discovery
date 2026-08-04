"""The read-only match preview endpoint.

Integration tests inside a rolled-back transaction. The endpoint must explain
its verdicts and store nothing, so both are asserted directly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ApplicationStatus, Seniority, SkillKind
from app.models.application import Application
from app.models.job import JobMatch
from app.models.profile import ResumeSkill
from app.matching.version import SCORING_VERSION
from tests.conftest import TEST_RECORD_MARKER
from tests.factories import make_job, make_profile, make_resume, make_user

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

DESCRIPTION = """About the role
We are hiring a Financial Analyst to own forecasting and the monthly close.

Requirements:
- 3+ years of experience in financial analysis
- Advanced Excel
- SQL
- Forecasting and budgeting

Preferred qualifications:
- Power BI

Additional context about the team follows so the description is long enough
to be scored reliably rather than rejected as a teaser.
"""


async def analysed_resume(session: AsyncSession):
    user = await make_user(session)
    resume = await make_resume(session, user=user)
    profile = await make_profile(session, resume=resume)
    profile.years_experience = 5.0
    profile.seniority = Seniority.MID
    profile.education = ["Bachelor of Science in Finance"]

    for key in ("excel", "sql", "forecasting", "budgeting", "financial modeling"):
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
    return user, resume


def url_for(resume_id: uuid.UUID) -> str:
    return f"/api/v1/resumes/{resume_id}/match-preview"


async def test_the_preview_scores_a_matching_job_and_explains_it(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, resume = await analysed_resume(db_session)
    await make_job(
        db_session, title="Senior Financial Analyst", description=DESCRIPTION
    )

    response = await api_client.post(url_for(resume.id), json={"limit": 5})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["target_role"] == "Financial Analyst"
    assert data["threshold"] == 70.0
    assert data["scoring_version"] == SCORING_VERSION
    assert data["qualified"] == 1

    job = data["jobs"][0]
    assert job["qualified"] is True
    assert job["final_score"] >= 70.0
    assert job["role"]["verdict"] == "exact"
    assert job["role"]["reason"]
    assert job["skills"]["matched_required"]
    assert job["positive_evidence"]
    assert set(job["components"]) == {
        "role",
        "required_skills",
        "preferred_skills",
        "experience",
        "domain_education",
        "supporting",
    }


async def test_the_preview_shows_why_a_job_was_rejected(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, resume = await analysed_resume(db_session)
    await make_job(db_session, title="Accountant", description=DESCRIPTION)

    response = await api_client.post(url_for(resume.id), json={"limit": 5})

    assert response.status_code == 200
    job = response.json()["data"]["jobs"][0]
    assert job["qualified"] is False
    assert job["rejection_code"] == "role_family_mismatch"
    assert job["rejection_reason"]
    assert job["components"]["role"]["score"] == 0.0


async def test_rejected_jobs_can_be_hidden(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, resume = await analysed_resume(db_session)
    await make_job(db_session, title="Accountant", description=DESCRIPTION)
    await make_job(db_session, title="Financial Analyst", description=DESCRIPTION)

    response = await api_client.post(
        url_for(resume.id), json={"limit": 5, "include_rejected": False}
    )

    data = response.json()["data"]
    assert data["evaluated"] == 2
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["qualified"] is True


async def test_the_preview_stores_nothing(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, resume = await analysed_resume(db_session)
    await make_job(db_session, description=DESCRIPTION)

    await api_client.post(url_for(resume.id), json={"limit": 5})

    assert await db_session.scalar(select(func.count()).select_from(JobMatch)) == 0


async def test_an_applied_job_is_reported_as_not_presentable(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user, resume = await analysed_resume(db_session)
    job = await make_job(db_session, description=DESCRIPTION)
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

    response = await api_client.post(url_for(resume.id), json={"limit": 5})

    data = response.json()["data"]
    assert data["excluded_as_applied"] == 1
    assert data["presentable"] == 0

    item = data["jobs"][0]
    assert item["qualified"] is True
    assert item["is_presentable"] is False
    assert item["is_eligible"] is False
    assert "history" in item["eligibility_reason"]


async def test_specific_jobs_can_be_requested(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, resume = await analysed_resume(db_session)
    wanted = await make_job(db_session, description=DESCRIPTION)
    await make_job(db_session, title="Financial Analyst II", description=DESCRIPTION)

    response = await api_client.post(
        url_for(resume.id), json={"job_ids": [str(wanted.id)], "limit": 10}
    )

    data = response.json()["data"]
    assert data["evaluated"] == 1
    assert data["jobs"][0]["job_id"] == str(wanted.id)


async def test_an_unanalysed_resume_returns_404(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    resume = await make_resume(db_session, user=user)

    response = await api_client.post(url_for(resume.id), json={"limit": 5})

    assert response.status_code == 404
    assert response.json()["errors"][0]["code"] == "resume_profile_not_found"


async def test_a_resume_with_no_target_role_returns_422(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    resume = await make_resume(db_session, user=user)
    profile = await make_profile(db_session, resume=resume)
    profile.detected_target_role = None
    await db_session.flush()

    response = await api_client.post(url_for(resume.id), json={"limit": 5})

    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "no_target_role"


async def test_the_limit_is_capped_server_side(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, resume = await analysed_resume(db_session)

    response = await api_client.post(url_for(resume.id), json={"limit": 500})

    assert response.status_code == 422


async def test_no_stored_jobs_is_reported_plainly(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, resume = await analysed_resume(db_session)

    response = await api_client.post(url_for(resume.id), json={"limit": 5})

    payload = response.json()
    assert payload["data"]["evaluated"] == 0
    assert payload["data"]["jobs"] == []
    assert "No stored jobs" in payload["message"]


# The structural guard against auto-apply concepts re-entering the API lives in
# tests/test_openapi.py and covers this endpoint too, so it is not repeated
# here.
