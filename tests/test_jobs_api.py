"""Integration tests for the job endpoints.

Every test runs inside a transaction that is rolled back, so nothing is
committed to resume_job_discovery.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.constants import JobUrlKind
from app.models.user import User
from app.services.user_service import get_or_create_default_user
from tests.factories import (
    make_job,
    make_job_source,
    make_match,
    make_portal,
    make_profile,
    make_resume,
    make_user,
)

BASE = "/api/v1/jobs"


async def _acting_user(session: AsyncSession, settings: Settings) -> User:
    return await get_or_create_default_user(session, settings)


# --- Scoping ----------------------------------------------------------------


async def test_resume_id_is_required(api_client: AsyncClient) -> None:
    # Scores and statuses are per resume; listing without one would mix them.
    response = await api_client.get(BASE)

    assert response.status_code == 422
    assert response.json()["success"] is False


async def test_only_this_resumes_match_is_shown(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    mine = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=mine)
    theirs = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=theirs)

    portal = await make_portal(db_session, slug="alpha")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=portal)

    await make_match(db_session, resume=mine, job=job, final_score=92.0)
    await make_match(db_session, resume=theirs, job=job, final_score=71.0)

    response = await api_client.get(BASE, params={"resume_id": str(mine.id)})

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["match"]["final_score"] == 92.0


async def test_another_users_resume_is_not_found(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    other = await make_user(db_session)
    resume = await make_resume(db_session, user=other)

    response = await api_client.get(BASE, params={"resume_id": str(resume.id)})

    assert response.status_code == 404


# --- Canonical jobs and sources --------------------------------------------


async def test_a_job_on_two_portals_appears_once_with_both_sources(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    first = await make_portal(db_session, slug="alpha")
    second = await make_portal(db_session, slug="beta")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=first)
    await make_job_source(db_session, job=job, portal=second)
    await make_match(db_session, resume=resume, job=job)

    response = await api_client.get(BASE, params={"resume_id": str(resume.id)})

    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert sorted(items[0]["portals"]) == sorted([first.slug, second.slug])


async def test_the_most_direct_url_is_surfaced(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    portal = await make_portal(db_session, slug="alpha")
    job = await make_job(
        db_session, preferred_application_url="https://careers.acme.invalid/1"
    )
    await make_job_source(db_session, job=job, portal=portal)

    response = await api_client.get(BASE, params={"resume_id": str(resume.id)})

    item = response.json()["data"]["items"][0]
    assert item["application_url"] == "https://careers.acme.invalid/1"
    assert item["url_kind"] == JobUrlKind.EMPLOYER_CAREER.value


async def test_a_job_without_a_preferred_url_still_gives_the_user_a_link(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # A row the user cannot open is useless; fall back to the source URL.
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    portal = await make_portal(db_session, slug="alpha")
    job = await make_job(db_session)
    source = await make_job_source(db_session, job=job, portal=portal)

    response = await api_client.get(BASE, params={"resume_id": str(resume.id)})

    assert response.json()["data"]["items"][0]["application_url"] == source.source_url


async def test_job_detail_lists_every_source(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    first = await make_portal(db_session, slug="alpha")
    second = await make_portal(db_session, slug="beta")
    job = await make_job(db_session)
    await make_job_source(
        db_session,
        job=job,
        portal=first,
        external_id="ext-1",
        application_url="https://boards.example.invalid/apply/1",
        url_kind=JobUrlKind.ATS,
        is_preferred=True,
    )
    await make_job_source(db_session, job=job, portal=second, external_id="ext-2")

    response = await api_client.get(
        f"{BASE}/{job.id}", params={"resume_id": str(resume.id)}
    )

    assert response.status_code == 200
    detail = response.json()["data"]
    assert len(detail["sources"]) == 2
    assert detail["canonical_key"] == job.canonical_key
    preferred = [s for s in detail["sources"] if s["is_preferred"]]
    assert preferred[0]["url_kind"] == JobUrlKind.ATS.value


async def test_an_unknown_job_is_404(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    response = await api_client.get(
        f"{BASE}/{uuid.uuid4()}", params={"resume_id": str(resume.id)}
    )

    assert response.status_code == 404


# --- Filtering and sorting --------------------------------------------------


async def test_expired_jobs_are_hidden_by_default(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    portal = await make_portal(db_session, slug="alpha")
    active = await make_job(db_session, title="Financial Analyst")
    expired = await make_job(
        db_session, title="Stale Analyst", expired_at=datetime.now(UTC)
    )
    for job in (active, expired):
        await make_job_source(db_session, job=job, portal=portal)
        await make_match(db_session, resume=resume, job=job)

    default = await api_client.get(BASE, params={"resume_id": str(resume.id)})
    included = await api_client.get(
        BASE, params={"resume_id": str(resume.id), "include_expired": True}
    )

    assert [item["title"] for item in default.json()["data"]["items"]] == [
        "Financial Analyst"
    ]
    assert len(included.json()["data"]["items"]) == 2


async def test_jobs_can_be_filtered_by_portal(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    first = await make_portal(db_session, slug="alpha")
    second = await make_portal(db_session, slug="beta")
    one = await make_job(db_session, title="From Alpha")
    two = await make_job(db_session, title="From Beta")
    await make_job_source(db_session, job=one, portal=first)
    await make_job_source(db_session, job=two, portal=second)
    for job in (one, two):
        await make_match(db_session, resume=resume, job=job)

    response = await api_client.get(
        BASE, params={"resume_id": str(resume.id), "portal": second.slug}
    )

    items = response.json()["data"]["items"]
    assert [item["title"] for item in items] == ["From Beta"]


async def test_newest_first_sorts_by_posted_date(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    portal = await make_portal(db_session, slug="alpha")
    now = datetime.now(UTC)
    older = await make_job(
        db_session, title="Older", posted_at=now - timedelta(days=7)
    )
    newer = await make_job(
        db_session, title="Newer", posted_at=now - timedelta(hours=2)
    )
    for job in (older, newer):
        await make_job_source(db_session, job=job, portal=portal)
        await make_match(db_session, resume=resume, job=job)

    response = await api_client.get(
        BASE, params={"resume_id": str(resume.id), "sort": "newest_first"}
    )

    titles = [item["title"] for item in response.json()["data"]["items"]]
    assert titles == ["Newer", "Older"]


async def test_best_overall_can_rank_an_older_strong_match_first(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Freshness helps prioritise; it does not override relevance.
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    portal = await make_portal(db_session, slug="alpha")
    now = datetime.now(UTC)
    strong_old = await make_job(
        db_session, title="Strong but a week old", posted_at=now - timedelta(days=7)
    )
    weak_new = await make_job(
        db_session, title="Weak but minutes old", posted_at=now
    )
    await make_job_source(db_session, job=strong_old, portal=portal)
    await make_job_source(db_session, job=weak_new, portal=portal)
    await make_match(
        db_session, resume=resume, job=strong_old, final_score=94.0, ranking_score=88.0
    )
    await make_match(
        db_session, resume=resume, job=weak_new, final_score=71.0, ranking_score=75.0
    )

    response = await api_client.get(
        BASE, params={"resume_id": str(resume.id), "sort": "best_overall"}
    )

    titles = [item["title"] for item in response.json()["data"]["items"]]
    assert titles[0] == "Strong but a week old"


async def test_a_job_scored_in_several_runs_is_listed_once(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    portal = await make_portal(db_session, slug="alpha")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=portal)

    # Two rescores of the same job for the same resume, outside any run.
    now = datetime.now(UTC)
    await make_match(
        db_session,
        resume=resume,
        job=job,
        final_score=80.0,
        matched_at=now - timedelta(hours=1),
    )
    await make_match(
        db_session, resume=resume, job=job, final_score=88.0, matched_at=now
    )

    response = await api_client.get(BASE, params={"resume_id": str(resume.id)})

    body = response.json()["data"]
    assert len(body["items"]) == 1
    assert body["pagination"]["total"] == 1
    # The newest verdict wins; the earlier one is retained but not shown.
    assert body["items"][0]["match"]["final_score"] == 88.0


async def test_a_job_with_no_score_yet_is_still_listable(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    portal = await make_portal(db_session, slug="alpha")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=portal)

    response = await api_client.get(BASE, params={"resume_id": str(resume.id)})

    item = response.json()["data"]["items"][0]
    assert item["match"] is None
    assert item["application_status"] == "not_applied"
