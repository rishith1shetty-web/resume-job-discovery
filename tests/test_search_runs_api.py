"""Integration tests for the search-run endpoints.

``POST /search-runs`` is the Find Matches endpoint: it creates the run and
executes it synchronously. Portals are deterministic fakes registered on the
process registry for the duration of one test, so nothing here reaches a
network.

Every test runs inside a transaction that is rolled back, so nothing is
committed to resume_job_discovery. Records carry the pytest marker. Nothing
here truncates, drops or resets anything.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.constants import (
    MAX_ENABLED_PORTALS,
    ApplicationStatus,
    Seniority,
    SkillKind,
)
from app.models.application import Application
from app.models.profile import ResumeSkill
from app.models.search import SearchRun
from app.models.user import User
from app.portals.exceptions import PortalBlockedError
from app.portals.registry import registry
from app.search.freshness import is_eligible_age
from app.services.user_service import get_or_create_default_user
from tests.conftest import TEST_RECORD_MARKER
from tests.factories import make_portal, make_profile, make_resume, make_user
from tests.fake_portals import (
    build_fake_portal,
    make_jobs,
    make_raw_job,
    temporary_registry,
)

BASE = "/api/v1/search-runs"

#: The clock these fixtures are anchored to.
#:
#: Real time, deliberately, and this is the one module in the search tests that
#: has no choice. Every test here drives the run through ``POST /search-runs``
#: over HTTP, and the route takes no clock: ``execute_search_run`` falls back to
#: ``datetime.now(UTC)``, so the 30-day eligibility gate is evaluated against
#: the wall clock no matter what the fixtures say.
#:
#: ``tests/test_search_orchestration.py`` freezes an equivalent constant and is
#: right to - it calls ``execute_search_run(now=NOW)`` directly, so fixture time
#: and gate time are the same frozen instant. Copying that literal into a module
#: that cannot inject it is what broke these tests: the fixtures stayed pinned to
#: 2026-08-04 while the gate moved on, and on 2026-09-03 every listing here
#: silently aged past ``MAX_ELIGIBLE_AGE`` and the runs started returning
#: nothing.
#:
#: Read once at import so a single run cannot drift mid-suite, and never a
#: calendar literal, because a literal only postpones the same failure.
NOW = datetime.now(UTC)


async def _acting_user(session: AsyncSession, settings: Settings) -> User:
    """The user the API acts as, so fixtures and requests agree."""
    return await get_or_create_default_user(session, settings)


async def _ready_resume(session: AsyncSession, settings: Settings) -> uuid.UUID:
    """An analysed, scoreable resume belonging to the acting user."""
    user = await _acting_user(session, settings)
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
    return resume.id


async def _searchable_portal(
    session: AsyncSession, *, slug: str, adapter_class
):
    """A portal row wired to a registered fake adapter."""
    return await make_portal(
        session, slug=slug, adapter_key=adapter_class.slug, options={}
    )


# --- The Find Matches flow --------------------------------------------------


def test_the_fixture_clock_is_inside_the_eligibility_window() -> None:
    """These fixtures must be old enough to be real and new enough to qualify.

    Asserted against production's own gate rather than against a number, so it
    cannot drift from the rule it protects. It fails the moment someone pins
    ``NOW`` back to a calendar date, which is the mistake that made six tests in
    this module return zero results - and which they reported only as a bare
    ``assert 0 == 6``, naming neither dates nor freshness.
    """
    assert is_eligible_age(NOW, datetime.now(UTC)), (
        "Fixture jobs are outside the 30-day window, so every search in this "
        "module will qualify nothing. NOW must track the wall clock: this "
        "module posts over HTTP and cannot inject a clock into the gate."
    )


async def test_a_search_returns_qualified_jobs(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-solo", jobs=make_jobs("api-solo", 6, now=NOW))
    portal = await _searchable_portal(db_session, slug="solo", adapter_class=adapter)

    with temporary_registry(registry, adapter):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["success"] is True
    assert body["errors"] == []

    data = body["data"]
    assert data["status"] == "completed"
    assert data["requested_limit"] == 20
    assert data["returned_count"] == 6
    assert data["min_score"] == 70
    assert len(data["results"]) == 6


async def test_each_result_carries_what_the_dashboard_shows(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-cols", jobs=make_jobs("api-cols", 2, now=NOW))
    portal = await _searchable_portal(db_session, slug="cols", adapter_class=adapter)

    with temporary_registry(registry, adapter):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    job = response.json()["data"]["results"][0]

    # CLAUDE.md section 10's required columns.
    assert job["title"]
    assert job["company"]
    assert job["location"]
    assert job["portal_slugs"]
    assert job["posted_at"]
    assert job["final_score"] >= 70
    assert job["qualified"] is True
    assert job["preferred_application_url"]
    assert job["application_status"] == "not_applied"
    assert job["match_explanation"]


async def test_a_short_result_set_is_reported_as_short(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-thin", jobs=make_jobs("api-thin", 2, now=NOW))
    portal = await _searchable_portal(db_session, slug="thin", adapter_class=adapter)

    with temporary_registry(registry, adapter):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    body = response.json()
    assert body["data"]["is_short"] is True
    assert body["data"]["returned_count"] == 2
    assert "never lowered" in body["message"]


async def test_no_qualifying_jobs_is_stated_plainly(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal(
        "api-none", jobs=make_jobs("api-none", 4, title="Data Engineer", now=NOW)
    )
    portal = await _searchable_portal(db_session, slug="none", adapter_class=adapter)

    with temporary_registry(registry, adapter):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    body = response.json()
    assert body["data"]["returned_count"] == 0
    assert body["data"]["results"] == []
    assert "70%" in body["message"]


async def test_the_api_returns_the_top_n_of_one_combined_pool(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Phase 12: the request is no longer divided between portals.

    Both portals here collect everything they have; the returned twenty are
    the best of the combined forty, and the summary says so.
    """
    resume_id = await _ready_resume(db_session, settings)
    first = build_fake_portal("api-a", jobs=make_jobs("api-a", 20, now=NOW))
    second = build_fake_portal("api-b", jobs=make_jobs("api-b", 20, now=NOW))
    left = await _searchable_portal(db_session, slug="a", adapter_class=first)
    right = await _searchable_portal(db_session, slug="b", adapter_class=second)

    with temporary_registry(registry, first, second):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(left.id), str(right.id)],
                "requested_limit": 20,
            },
        )

    data = response.json()["data"]
    assert data["returned_count"] == 20
    assert sum(leg["selected_count"] for leg in data["portals"]) == 20
    # Both portals searched fully rather than to a share of the request.
    assert [leg["raw_count"] for leg in data["portals"]] == [20, 20]

    summary = data["summary"]
    assert summary["search_strategy"] == "deep_unified"
    assert summary["candidate_pool_size"] == 40
    assert summary["portals_exhausted"] == 2


async def test_a_failing_portal_completes_the_run_with_errors(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # A blocked portal is reported, not hidden.
    resume_id = await _ready_resume(db_session, settings)
    good = build_fake_portal("api-good", jobs=make_jobs("api-good", 5, now=NOW))
    blocked = build_fake_portal(
        "api-blocked",
        error=PortalBlockedError("Access is not authorized.", portal="api-blocked"),
    )
    ok_portal = await _searchable_portal(db_session, slug="good", adapter_class=good)
    bad_portal = await _searchable_portal(
        db_session, slug="blocked", adapter_class=blocked
    )

    with temporary_registry(registry, good, blocked):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(ok_portal.id), str(bad_portal.id)],
                "requested_limit": 20,
            },
        )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "completed_with_errors"
    assert data["returned_count"] == 5
    assert any("not authorized" in warning for warning in data["warnings"])

    failed = [leg for leg in data["portals"] if leg["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["error"]


async def test_every_portal_failing_reports_a_failed_run(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    blocked = build_fake_portal(
        "api-dead", error=PortalBlockedError("blocked", portal="api-dead")
    )
    portal = await _searchable_portal(db_session, slug="dead", adapter_class=blocked)

    with temporary_registry(registry, blocked):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert data["returned_count"] == 0
    assert data["warnings"]


async def test_an_applied_job_is_not_offered_again(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume_id = await _ready_resume(db_session, settings)
    listing = make_raw_job(portal="api-hist", index=0, company="Hist Co", now=NOW)
    adapter = build_fake_portal("api-hist", jobs=[listing])
    portal = await _searchable_portal(db_session, slug="hist", adapter_class=adapter)

    with temporary_registry(registry, adapter):
        first = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )
        job_id = first.json()["data"]["results"][0]["job_id"]

        db_session.add(
            Application(
                user_id=user.id,
                resume_id=resume_id,
                job_id=uuid.UUID(job_id),
                status=ApplicationStatus.APPLIED,
                applied_at=NOW,
                notes=TEST_RECORD_MARKER,
            )
        )
        await db_session.commit()

        second = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    assert second.json()["data"]["returned_count"] == 0


async def test_the_executed_queries_are_reported(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-q", jobs=make_jobs("api-q", 3, now=NOW))
    portal = await _searchable_portal(db_session, slug="q", adapter_class=adapter)

    with temporary_registry(registry, adapter):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    data = response.json()["data"]
    assert data["queries"]
    assert data["queries"][0]["query_text"] == "Financial Analyst"
    assert data["target_role"] == "Financial Analyst"


async def test_no_portal_configuration_is_exposed(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Adapter options are opaque operator configuration, not user-facing data.
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-secret", jobs=make_jobs("api-secret", 2, now=NOW))
    portal = await make_portal(
        db_session,
        slug="secret",
        adapter_key=adapter.slug,
        options={"api_token": "super-secret-value", "access_authorized": False},
    )

    with temporary_registry(registry, adapter):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
            },
        )

    assert "super-secret-value" not in response.text
    assert "api_token" not in response.text
    assert "raw_payload" not in response.text


# --- Validation -------------------------------------------------------------


async def test_an_unsupported_limit_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-v1", jobs=[])
    portal = await _searchable_portal(db_session, slug="v1", adapter_class=adapter)

    with temporary_registry(registry, adapter):
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


async def _post_portal_ids(
    api_client: AsyncClient, resume_id: uuid.UUID, count: int
) -> AsyncClient:
    return await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(uuid.uuid4()) for _ in range(count)],
            "requested_limit": 100,
        },
    )


async def test_more_than_the_ceiling_of_portals_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """One past the ceiling is refused for being too many, and says so."""
    resume_id = await _ready_resume(db_session, settings)

    response = await _post_portal_ids(
        api_client, resume_id, MAX_ENABLED_PORTALS + 1
    )

    assert response.status_code == 422
    error = response.json()["errors"][0]
    assert error["code"] == "too_long"
    assert error["field"] == "body.selected_portal_ids"
    assert str(MAX_ENABLED_PORTALS) in error["message"]


@pytest.mark.parametrize("count", [1, 8, 9, MAX_ENABLED_PORTALS])
async def test_up_to_the_ceiling_passes_the_count_check(
    api_client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    count: int,
) -> None:
    """Counts at or below the ceiling are never refused *for their length*.

    The ids are random, so these still fail - as `portal_not_found`, which is
    the point. Asserting on the code rather than the status is what makes this
    a real boundary test: a plain `== 422` would pass just as happily if the
    length check had rejected them, which is exactly the false positive that
    raising a maximum invites.

    8 is the previous ceiling and must keep working; 9 is the count that used
    to be refused and no longer is.
    """
    resume_id = await _ready_resume(db_session, settings)

    response = await _post_portal_ids(api_client, resume_id, count)

    codes = {error["code"] for error in response.json()["errors"]}
    assert "too_long" not in codes, f"{count} portals was refused for length"
    assert codes == {"portal_not_found"}


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


async def test_a_portal_without_an_adapter_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # A portal row may exist before its adapter does. Selecting it would create
    # a leg that can only fail, which reads as "broken" rather than "not built".
    resume_id = await _ready_resume(db_session, settings)
    portal = await make_portal(db_session, slug="planned")

    response = await api_client.post(
        BASE,
        json={
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(portal.id)],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "portal_not_implemented"


async def test_an_unanalyzed_resume_cannot_be_searched(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    adapter = build_fake_portal("api-v2", jobs=[])
    portal = await _searchable_portal(db_session, slug="v2", adapter_class=adapter)

    with temporary_registry(registry, adapter):
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


async def test_the_threshold_is_not_client_settable(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-lock", jobs=make_jobs("api-lock", 2, now=NOW))
    portal = await _searchable_portal(db_session, slug="lock", adapter_class=adapter)

    with temporary_registry(registry, adapter):
        response = await api_client.post(
            BASE,
            json={
                "resume_id": str(resume_id),
                "selected_portal_ids": [str(portal.id)],
                "requested_limit": 20,
                # Ignored: the threshold is not part of the request contract.
                "min_score": 40,
            },
        )

    assert response.status_code == 201
    assert response.json()["data"]["min_score"] == 70


# --- Reading a run back -----------------------------------------------------


async def test_a_run_can_be_read_back(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-read", jobs=make_jobs("api-read", 3, now=NOW))
    portal = await _searchable_portal(db_session, slug="read", adapter_class=adapter)

    with temporary_registry(registry, adapter):
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

    response = await api_client.get(f"{BASE}/{created['search_run_id']}")

    assert response.status_code == 200
    assert response.json()["data"]["id"] == created["search_run_id"]
    assert response.json()["data"]["status"] == "completed"


async def test_the_results_endpoint_returns_the_same_order(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-order", jobs=make_jobs("api-order", 5, now=NOW))
    portal = await _searchable_portal(db_session, slug="order", adapter_class=adapter)

    with temporary_registry(registry, adapter):
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

    response = await api_client.get(
        f"{BASE}/{created['search_run_id']}/results"
    )

    assert response.status_code == 200
    stored = response.json()["data"]
    assert [job["job_id"] for job in stored["results"]] == [
        job["job_id"] for job in created["results"]
    ]
    assert [job["final_score"] for job in stored["results"]] == [
        job["final_score"] for job in created["results"]
    ]


async def test_results_for_an_unknown_run_are_404(api_client: AsyncClient) -> None:
    response = await api_client.get(f"{BASE}/{uuid.uuid4()}/results")

    assert response.status_code == 404
    assert response.json()["success"] is False


async def test_progress_reports_per_portal(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    first = build_fake_portal("api-p0", jobs=make_jobs("api-p0", 3, now=NOW))
    second = build_fake_portal("api-p1", jobs=make_jobs("api-p1", 3, now=NOW))
    left = await _searchable_portal(db_session, slug="p0", adapter_class=first)
    right = await _searchable_portal(db_session, slug="p1", adapter_class=second)

    with temporary_registry(registry, first, second):
        created = (
            await api_client.post(
                BASE,
                json={
                    "resume_id": str(resume_id),
                    "selected_portal_ids": [str(left.id), str(right.id)],
                    "requested_limit": 20,
                },
            )
        ).json()["data"]

    response = await api_client.get(
        f"{BASE}/{created['search_run_id']}/progress"
    )

    assert response.status_code == 200
    progress = response.json()["data"]
    assert progress["portal_count"] == 2
    assert progress["portals_completed"] == 2
    assert progress["portals_failed"] == 0
    assert progress["is_terminal"] is True


async def test_runs_are_listed_per_resume(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Two resumes for the same user must not see each other's runs.
    user = await _acting_user(db_session, settings)
    first = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=first)
    second = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=second)

    adapter = build_fake_portal("api-list", jobs=[])
    portal = await _searchable_portal(db_session, slug="list", adapter_class=adapter)

    with temporary_registry(registry, adapter):
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


async def test_a_second_search_leaves_the_first_run_intact(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    resume_id = await _ready_resume(db_session, settings)
    adapter = build_fake_portal("api-hist2", jobs=make_jobs("api-hist2", 3, now=NOW))
    portal = await _searchable_portal(db_session, slug="hist2", adapter_class=adapter)

    with temporary_registry(registry, adapter):
        payload = {
            "resume_id": str(resume_id),
            "selected_portal_ids": [str(portal.id)],
            "requested_limit": 20,
        }
        first = (await api_client.post(BASE, json=payload)).json()["data"]
        second = (await api_client.post(BASE, json=payload)).json()["data"]

    assert first["search_run_id"] != second["search_run_id"]

    earlier = await api_client.get(f"{BASE}/{first['search_run_id']}/results")
    assert earlier.status_code == 200
    assert [job["job_id"] for job in earlier.json()["data"]["results"]] == [
        job["job_id"] for job in first["results"]
    ]

    runs = await db_session.scalar(
        select(SearchRun).where(SearchRun.id == uuid.UUID(first["search_run_id"]))
    )
    assert runs is not None
    assert runs.completed_at is not None
