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
from app.core.constants import ApplicationStatus, JobUrlKind
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
    await make_match(db_session, resume=resume, job=job)

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
    await make_match(db_session, resume=resume, job=job)

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


async def test_best_overall_ranks_the_newer_job_first(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Recency is the primary sort; match quality only breaks ties. Both jobs
    # are above the threshold and inside the window, so both are worth
    # applying to and the newer one leads.
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
    assert titles == ["Weak but minutes old", "Strong but a week old"]


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


async def test_a_job_never_scored_for_this_resume_is_not_listed(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """This used to assert the opposite, which was the scoping defect.

    A stored job that this resume was never scored against, and that the user
    never applied to under this resume, does not belong to this resume. Listing
    it meant every resume saw every job in the database.
    """
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    portal = await make_portal(db_session, slug="alpha")
    job = await make_job(db_session, title="Scored for nobody")
    await make_job_source(db_session, job=job, portal=portal)

    response = await api_client.get(BASE, params={"resume_id": str(resume.id)})

    titles = [item["title"] for item in response.json()["data"]["items"]]
    assert "Scored for nobody" not in titles


# --- Resume scoping (regression) --------------------------------------------
#
# GET /jobs outer-joins the match and the application, and for a long time
# nothing else constrained the row, so it returned every job in the database
# whatever resume was asked for. One resume's list was therefore decided by
# another resume's searches - including its order, once ranking became
# newest-first.


async def test_another_resumes_jobs_never_appear(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    mine = await make_resume(db_session, user=user)
    theirs = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=mine)
    await make_profile(db_session, resume=theirs)

    now = datetime.now(UTC)
    my_job = await make_job(db_session, title="Mine, scored for this resume")
    their_job = await make_job(db_session, title="Theirs, never scored for mine")
    await make_match(
        db_session, resume=mine, job=my_job, final_score=80.0, ranking_score=80.0
    )
    await make_match(
        db_session, resume=theirs, job=their_job, final_score=99.0, ranking_score=99.0
    )

    response = await api_client.get(BASE, params={"resume_id": str(mine.id)})
    titles = [item["title"] for item in response.json()["data"]["items"]]

    assert "Mine, scored for this resume" in titles
    assert "Theirs, never scored for mine" not in titles


async def test_another_resumes_newer_job_cannot_take_the_top_row(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Ordering, not just membership: leakage used to decide position too."""
    user = await _acting_user(db_session, settings)
    mine = await make_resume(db_session, user=user)
    theirs = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=mine)
    await make_profile(db_session, resume=theirs)

    now = datetime.now(UTC)
    my_newest = await make_job(
        db_session, title="My newest", posted_at=now - timedelta(hours=1)
    )
    my_older = await make_job(
        db_session, title="My older", posted_at=now - timedelta(days=10)
    )
    # Newer than anything of mine, and a better score. It must still not appear.
    their_newest = await make_job(
        db_session, title="Their newest", posted_at=now - timedelta(minutes=1)
    )
    for resume, job, score in (
        (mine, my_newest, 75.0),
        (mine, my_older, 99.0),
        (theirs, their_newest, 99.0),
    ):
        await make_match(
            db_session, resume=resume, job=job, final_score=score, ranking_score=score
        )

    response = await api_client.get(
        BASE, params={"resume_id": str(mine.id), "sort": "best_overall"}
    )
    titles = [item["title"] for item in response.json()["data"]["items"]]

    assert "Their newest" not in titles
    # Newest first, match second: the 75% posted an hour ago leads the 99%
    # posted ten days ago.
    assert titles.index("My newest") < titles.index("My older")


async def test_an_applied_job_stays_listed_even_without_a_stored_match(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """The scoping filter must not drop application history."""
    from app.models.application import Application

    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    job = await make_job(db_session, title="Applied, never rescored")
    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job.id,
            status=ApplicationStatus.APPLIED,
            applied_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    response = await api_client.get(BASE, params={"resume_id": str(resume.id)})
    titles = [item["title"] for item in response.json()["data"]["items"]]

    assert "Applied, never rescored" in titles


# --- Rejection reasons serialise ---------------------------------------------
#
# The scorer writes objects into ``job_matches.rejection_reasons`` - a code and
# the sentence shown to the user - while the response schema declared a list of
# plain strings. Any page containing a rejected match therefore failed response
# validation and returned 500, which is why the dashboard and the application
# board both worked around ``GET /jobs`` rather than trusting it.


async def test_a_rejected_match_serialises_instead_of_failing_the_page(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)
    portal = await make_portal(db_session, slug="rej-a")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=portal)
    match = await make_match(
        db_session, resume=resume, job=job, final_score=63.25, qualified=False
    )
    match.rejection_reasons = [
        {
            "code": "below_threshold",
            "reason": "Scored 63.25, below the locked 70% threshold.",
        }
    ]
    await db_session.flush()

    response = await api_client.get(f"/api/v1/jobs?resume_id={resume.id}")

    assert response.status_code == 200, response.text
    row = next(
        item for item in response.json()["data"]["items"] if item["id"] == str(job.id)
    )
    reasons = row["match"]["rejection_reasons"]
    assert reasons == [
        {
            "code": "below_threshold",
            "reason": "Scored 63.25, below the locked 70% threshold.",
        }
    ]


async def test_a_legacy_string_rejection_reason_still_serialises(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Rows written before the scorer recorded codes must not 500 either."""
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)
    portal = await make_portal(db_session, slug="rej-b")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=portal)
    match = await make_match(
        db_session, resume=resume, job=job, final_score=50.0, qualified=False
    )
    match.rejection_reasons = ["scored below the threshold"]
    await db_session.flush()

    response = await api_client.get(f"/api/v1/jobs?resume_id={resume.id}")

    assert response.status_code == 200, response.text
    row = next(
        item for item in response.json()["data"]["items"] if item["id"] == str(job.id)
    )
    assert row["match"]["rejection_reasons"] == [
        {"code": "unspecified", "reason": "scored below the threshold"}
    ]
