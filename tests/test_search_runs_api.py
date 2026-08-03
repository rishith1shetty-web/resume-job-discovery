"""Integration tests for the search-run endpoints.

Every test runs inside a transaction that is rolled back, so nothing is
committed to resume_job_discovery. Records carry the pytest marker. Nothing
here truncates, drops or resets anything.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.search import SearchRun
from app.models.user import User
from app.services.user_service import get_or_create_default_user
from tests.factories import make_portal, make_profile, make_resume, make_user

BASE = "/api/v1/search-runs"


async def _acting_user(session: AsyncSession, settings: Settings) -> User:
    """The user the API acts as, so fixtures and requests agree."""
    return await get_or_create_default_user(session, settings)


async def _ready_resume(session: AsyncSession, settings: Settings) -> uuid.UUID:
    """An analysed resume belonging to the acting user."""
    user = await _acting_user(session, settings)
    resume = await make_resume(session, user=user)
    await make_profile(session, resume=resume)
    return resume.id


# --- Creation ---------------------------------------------------------------


async def test_creating_a_run_splits_the_limit_evenly(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    first = await make_portal(db_session, slug="alpha")
    second = await make_portal(db_session, slug="beta")

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(first.id), str(second.id)],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["success"] is True
    assert body["errors"] == []

    run = body["data"]
    assert run["requested_limit"] == 20
    assert run["status"] == "pending"
    assert [portal["target_quota"] for portal in run["portals"]] == [10, 10]


async def test_every_selected_portal_gets_a_row(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # A run must not stop at the first portal that could satisfy the limit.
    resume_id = await _ready_resume(db_session, settings)
    portals = [
        await make_portal(db_session, slug=f"p{index}") for index in range(4)
    ]

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(portal.id) for portal in portals],
            "requested_limit": 50,
        },
    )

    assert response.status_code == 201, response.text
    legs = response.json()["data"]["portals"]
    assert len(legs) == 4
    assert sum(leg["target_quota"] for leg in legs) == 50
    assert {leg["target_quota"] for leg in legs} == {12, 13}


async def test_the_threshold_is_locked_at_70(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    portal = await make_portal(db_session, slug="alpha")

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(portal.id)],
            "requested_limit": 20,
            # Ignored: the threshold is not client-settable.
            "min_score": 40,
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["min_score"] == 70


async def test_the_run_snapshots_the_searched_role(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Re-analysing the resume later must not rewrite this run's history.
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)
    portal = await make_portal(db_session, slug="alpha")

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume.id),
            "selected_portal_ids": [str(portal.id)],
            "requested_limit": 20,
        },
    )

    data = response.json()["data"]
    assert data["searched_domain"] == "finance"
    assert data["searched_target_role"] == "financial_analyst"


# --- Rejections -------------------------------------------------------------


async def test_an_unsupported_limit_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    portal = await make_portal(db_session, slug="alpha")

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(portal.id)],
            "requested_limit": 30,
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert any(
        error["field"] == "body.requested_limit" for error in body["errors"]
    ), body["errors"]


async def test_no_portals_selected_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 422


async def test_a_repeated_portal_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    portal = await make_portal(db_session, slug="alpha")

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(portal.id), str(portal.id)],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 422


async def test_more_than_eight_portals_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(uuid.uuid4()) for _ in range(9)],
            "requested_limit": 100,
        },
    )

    assert response.status_code == 422


async def test_an_unknown_portal_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(uuid.uuid4())],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "portal_not_found"


async def test_a_disabled_portal_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    portal = await make_portal(db_session, slug="off", is_enabled=False)

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(portal.id)],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "portal_disabled"


async def test_an_unanalyzed_resume_cannot_be_searched(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    portal = await make_portal(db_session, slug="alpha")

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume.id),
            "selected_portal_ids": [str(portal.id)],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "profile_missing"


async def test_another_users_resume_is_not_found(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    other = await make_user(db_session)
    resume = await make_resume(db_session, user=other)
    await make_profile(db_session, resume=resume)
    portal = await make_portal(db_session, slug="alpha")

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume.id),
            "selected_portal_ids": [str(portal.id)],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 404


# --- Reading ----------------------------------------------------------------


async def test_a_run_can_be_read_back(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    portal = await make_portal(db_session, slug="alpha")

    created = (
        await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )
    ).json()["data"]

    response = await api_client.get(f"{BASE}/{created['id']}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == created["id"]


async def test_progress_reports_per_portal(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    portals = [await make_portal(db_session, slug=f"p{i}") for i in range(2)]

    created = (
        await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(p.id) for p in portals],
                "requested_limit": 20,
            },
        )
    ).json()["data"]

    response = await api_client.get(f"{BASE}/{created['id']}/progress")

    assert response.status_code == 200
    progress = response.json()["data"]
    assert progress["portal_count"] == 2
    assert progress["portals_completed"] == 0
    assert progress["portals_failed"] == 0
    assert progress["is_terminal"] is False
    assert len(progress["portals"]) == 2


async def test_runs_are_listed_per_resume(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Two resumes for the same user must not see each other's runs.
    user = await _acting_user(db_session, settings)
    first = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=first)
    second = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=second)
    portal = await make_portal(db_session, slug="alpha")

    for resume in (first, second):
        await api_client.post(
            BASE,
            json={
                "resume_id": str(resume.id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    response = await api_client.get(BASE, params={"resume_id": str(first.id)})

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["resume_id"] == str(first.id)


async def test_an_unknown_run_is_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"{BASE}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["success"] is False


async def test_creating_a_run_does_not_start_collecting(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Phase 3 records the run only. Nothing contacts a portal until Phase 7.
    resume_id = await _ready_resume(db_session, settings)
    portal = await make_portal(db_session, slug="alpha")

    created = (
        await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )
    ).json()["data"]

    run = await db_session.scalar(
        select(SearchRun).where(SearchRun.id == uuid.UUID(created["id"]))
    )

    assert run is not None
    assert run.started_at is None
    assert run.completed_at is None
    assert created["portals"][0]["raw_count"] == 0
    assert created["portals"][0]["selected_count"] == 0
