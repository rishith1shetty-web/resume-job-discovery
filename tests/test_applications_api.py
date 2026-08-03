"""Integration tests for manual application tracking.

The behaviour under test is the product's core promise: a status the user set
survives rediscovery, expiry and re-scoring, and never leaks between resumes.

Every test runs inside a transaction that is rolled back.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.constants import ApplicationStatus
from app.models.application import Application, ApplicationStatusHistory
from app.models.user import User
from app.services.user_service import get_or_create_default_user
from tests.factories import (
    make_job,
    make_job_source,
    make_match,
    make_portal,
    make_profile,
    make_resume,
)

APPLICATIONS = "/api/v1/applications"
HISTORY = "/api/v1/application-history"
JOBS = "/api/v1/jobs"


async def _acting_user(session: AsyncSession, settings: Settings) -> User:
    return await get_or_create_default_user(session, settings)


async def _patch_status(
    client: AsyncClient,
    *,
    job_id: uuid.UUID,
    resume_id: uuid.UUID,
    status: str,
) -> Response:
    return await client.patch(
        f"{APPLICATIONS}/{job_id}",
        params={"resume_id": str(resume_id)},
        json={"status": status},
    )


async def _set_status(
    client: AsyncClient,
    *,
    job_id: uuid.UUID,
    resume_id: uuid.UUID,
    status: str,
) -> dict[str, Any]:
    """Set a status and insist it succeeded, returning the response body.

    The assertion belongs here: a test that ignores the status code passes
    even when every request is failing with a 500.
    """
    response = await _patch_status(
        client, job_id=job_id, resume_id=resume_id, status=status
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _history_count(session: AsyncSession, application_id: uuid.UUID) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(ApplicationStatusHistory)
        .where(ApplicationStatusHistory.application_id == application_id)
    )
    return int(total or 0)


# --- Setting a status -------------------------------------------------------


async def test_marking_applied_stamps_applied_at(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    body = await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="applied"
    )

    assert body["success"] is True
    assert body["data"]["status"] == "applied"
    assert body["data"]["applied_at"] is not None
    # The same job fields the list endpoint returns, so one schema does not
    # mean two different payloads.
    assert body["data"]["job_title"] == job.title


async def test_a_status_that_is_not_a_submission_leaves_applied_at_empty(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    body = await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="saved"
    )

    assert body["data"]["status"] == "saved"
    assert body["data"]["applied_at"] is None


async def test_moving_on_from_applied_keeps_the_original_timestamp(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    applied = await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="applied"
    )
    interview = await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="interview"
    )

    assert interview["data"]["status"] == "interview"
    assert interview["data"]["applied_at"] == applied["data"]["applied_at"]


async def test_expired_is_not_a_status_a_user_can_set(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    response = await _patch_status(
        api_client, job_id=job.id, resume_id=resume.id, status="expired"
    )

    # Expiry is a job condition; accepting it here would let a stale listing
    # overwrite APPLIED.
    assert response.status_code == 422
    assert response.json()["success"] is False


async def test_an_unknown_job_is_404(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)

    response = await _patch_status(
        api_client, job_id=uuid.uuid4(), resume_id=resume.id, status="saved"
    )

    assert response.status_code == 404


async def test_another_users_resume_is_404(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    from tests.factories import make_user

    other = await make_user(db_session)
    resume = await make_resume(db_session, user=other)
    job = await make_job(db_session)

    response = await _patch_status(
        api_client, job_id=job.id, resume_id=resume.id, status="saved"
    )

    assert response.status_code == 404


# --- History ----------------------------------------------------------------


async def test_every_transition_writes_one_history_row(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    for status in ("saved", "applied", "interview"):
        await _set_status(
            api_client, job_id=job.id, resume_id=resume.id, status=status
        )

    application = await db_session.scalar(
        select(Application).where(Application.job_id == job.id)
    )
    assert application is not None
    # One opening row (-> not_applied) plus the three transitions.
    assert await _history_count(db_session, application.id) == 4


async def test_repeating_the_same_status_writes_no_history(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="applied"
    )
    repeat = await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="applied"
    )

    assert repeat["message"] is not None

    application = await db_session.scalar(
        select(Application).where(Application.job_id == job.id)
    )
    assert application is not None
    # Opening row plus one real transition. The repeat added nothing.
    assert await _history_count(db_session, application.id) == 2


async def test_history_is_listed_newest_first(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    job = await make_job(db_session, title="Financial Analyst")

    for status in ("saved", "applied"):
        await _set_status(
            api_client, job_id=job.id, resume_id=resume.id, status=status
        )
        # Real users are seconds apart. In-process requests are not, and this
        # platform's clock is coarser than the gap between them, so without a
        # pause the two transitions would share a timestamp and neither order
        # would be wrong.
        await asyncio.sleep(0.05)

    response = await api_client.get(HISTORY, params={"resume_id": str(resume.id)})

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert items[0]["new_status"] == "applied"
    assert items[0]["old_status"] == "saved"
    assert items[0]["job_title"] == "Financial Analyst"


async def test_history_is_separated_by_resume(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    first = await make_resume(db_session, user=user)
    second = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    await _set_status(
        api_client, job_id=job.id, resume_id=first.id, status="applied"
    )

    response = await api_client.get(HISTORY, params={"resume_id": str(second.id)})

    assert response.json()["data"]["items"] == []


# --- The rules the product depends on ---------------------------------------


async def test_the_same_job_has_independent_status_per_resume(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    first = await make_resume(db_session, user=user)
    second = await make_resume(db_session, user=user)
    job = await make_job(db_session)

    await _set_status(
        api_client, job_id=job.id, resume_id=first.id, status="applied"
    )

    response = await api_client.get(
        APPLICATIONS, params={"resume_id": str(second.id)}
    )

    assert response.json()["data"]["items"] == []


async def test_rediscovery_through_another_portal_keeps_applied(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # The central duplicate rule: a job already marked APPLIED must not come
    # back as a new unapplied opportunity when a second portal surfaces it.
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)

    first_portal = await make_portal(db_session, slug="alpha")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=first_portal)
    await make_match(db_session, resume=resume, job=job)

    await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="applied"
    )

    # A second portal now carries the same canonical job.
    second_portal = await make_portal(db_session, slug="beta")
    await make_job_source(db_session, job=job, portal=second_portal)

    response = await api_client.get(JOBS, params={"resume_id": str(resume.id)})

    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["application_status"] == "applied"
    assert sorted(items[0]["portals"]) == sorted(
        [first_portal.slug, second_portal.slug]
    )


async def test_a_job_expiring_does_not_change_the_application_status(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    portal = await make_portal(db_session, slug="alpha")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=portal)

    await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="applied"
    )

    # The listing goes stale.
    job.expired_at = datetime.now(UTC)
    await db_session.flush()

    response = await api_client.get(
        APPLICATIONS, params={"resume_id": str(resume.id)}
    )

    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["status"] == ApplicationStatus.APPLIED.value


async def test_rescoring_does_not_reset_the_status(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)
    portal = await make_portal(db_session, slug="alpha")
    job = await make_job(db_session)
    await make_job_source(db_session, job=job, portal=portal)
    now = datetime.now(UTC)
    await make_match(
        db_session,
        resume=resume,
        job=job,
        final_score=80.0,
        matched_at=now - timedelta(hours=1),
    )

    await _set_status(
        api_client, job_id=job.id, resume_id=resume.id, status="applied"
    )

    # A later run rescores the same job.
    await make_match(
        db_session, resume=resume, job=job, final_score=91.0, matched_at=now
    )

    response = await api_client.get(JOBS, params={"resume_id": str(resume.id)})

    item = response.json()["data"]["items"][0]
    assert item["application_status"] == "applied"
    assert item["match"]["final_score"] == 91.0


async def test_applications_can_be_filtered_by_status(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    applied_job = await make_job(db_session, title="Applied one")
    saved_job = await make_job(db_session, title="Saved one")

    await _set_status(
        api_client, job_id=applied_job.id, resume_id=resume.id, status="applied"
    )
    await _set_status(
        api_client, job_id=saved_job.id, resume_id=resume.id, status="saved"
    )

    response = await api_client.get(
        APPLICATIONS, params={"resume_id": str(resume.id), "status": "saved"}
    )

    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["job_title"] == "Saved one"


async def test_jobs_can_be_filtered_by_application_status(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await _acting_user(db_session, settings)
    resume = await make_resume(db_session, user=user)
    await make_profile(db_session, resume=resume)
    portal = await make_portal(db_session, slug="alpha")

    applied_job = await make_job(db_session, title="Applied one")
    untouched = await make_job(db_session, title="Untouched")
    for job in (applied_job, untouched):
        await make_job_source(db_session, job=job, portal=portal)
        await make_match(db_session, resume=resume, job=job)

    await _set_status(
        api_client, job_id=applied_job.id, resume_id=resume.id, status="applied"
    )

    applied = await api_client.get(
        JOBS, params={"resume_id": str(resume.id), "status": "applied"}
    )
    not_applied = await api_client.get(
        JOBS, params={"resume_id": str(resume.id), "status": "not_applied"}
    )

    assert [item["title"] for item in applied.json()["data"]["items"]] == [
        "Applied one"
    ]
    # A job with no application row at all counts as not applied.
    assert [item["title"] for item in not_applied.json()["data"]["items"]] == [
        "Untouched"
    ]
