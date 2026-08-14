"""Manual application tracking (Phase 8).

The workflow after Find Matches: the user opens a link, applies on the
employer's own site, comes back and records what happened.

Every test runs inside a transaction that is rolled back, so nothing is
committed to resume_job_discovery. Records carry the pytest marker. Nothing
here truncates, drops or resets anything.

Nothing in this module - or in the code it exercises - opens, fills or submits
an application.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.constants import ApplicationStatus, Seniority, SkillKind
from app.models.application import Application, ApplicationStatusHistory
from app.models.profile import ResumeSkill
from app.models.search import SearchRun
from app.models.user import User
from app.services import application_service, search_run_service
from app.services.search_orchestrator import execute_search_run
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
from tests.fake_portals import build_fake_portal, make_raw_job

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

APPS = "/api/v1/applications"
HISTORY = "/api/v1/application-history"


async def acting_user(session: AsyncSession, settings: Settings) -> User:
    return await get_or_create_default_user(session, settings)


async def tracked_resume(session: AsyncSession, *, user: User):
    """An analysed, scoreable resume."""
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
    return resume


def url(job_id: uuid.UUID, resume_id: uuid.UUID) -> str:
    return f"{APPS}/{job_id}?resume_id={resume_id}"


# --- Creation and retrieval -------------------------------------------------


async def test_an_untracked_job_reads_as_not_applied(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Never having tracked a job is a normal state, not an error.
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    response = await api_client.get(url(job.id, resume.id))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["application"] is None
    assert data["status"] == "not_applied"
    assert data["history"] == []


async def test_the_first_status_creates_the_record(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    response = await api_client.patch(
        url(job.id, resume.id), json={"status": "saved"}
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "saved"
    assert data["resume_id"] == str(resume.id)
    assert data["job_id"] == str(job.id)

    rows = await db_session.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.resume_id == resume.id)
    )
    assert rows == 1


async def test_the_recorded_status_can_be_read_back(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(
        url(job.id, resume.id), json={"status": "applied", "notes": "Via referral"}
    )
    response = await api_client.get(url(job.id, resume.id))

    data = response.json()["data"]
    assert data["status"] == "applied"
    assert data["application"]["notes"] == "Via referral"
    assert data["application"]["applied_at"] is not None
    assert len(data["history"]) == 2  # opening row, then the transition


async def test_the_detail_view_carries_the_job_context(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(
        db_session,
        title="Financial Analyst",
        company="Acme Corporation",
        preferred_application_url="https://acme.invalid/careers/1",
    )

    await api_client.patch(url(job.id, resume.id), json={"status": "saved"})
    data = (await api_client.get(url(job.id, resume.id))).json()["data"]

    assert data["application"]["job_title"] == "Financial Analyst"
    assert data["application"]["company"] == "Acme Corporation"
    assert data["application"]["application_url"] == "https://acme.invalid/careers/1"


# --- One row, always --------------------------------------------------------


async def test_many_updates_never_create_a_second_row(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    for status in ("saved", "applied", "interview", "offer", "rejected"):
        response = await api_client.patch(
            url(job.id, resume.id), json={"status": status}
        )
        assert response.status_code == 200, response.text

    rows = await db_session.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.resume_id == resume.id, Application.job_id == job.id)
    )
    assert rows == 1


async def test_repeating_a_status_is_idempotent(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})
    first = (await api_client.get(url(job.id, resume.id))).json()["data"]

    repeat = await api_client.patch(
        url(job.id, resume.id), json={"status": "applied"}
    )
    second = (await api_client.get(url(job.id, resume.id))).json()["data"]

    assert "nothing changed" in repeat.json()["message"]
    assert len(second["history"]) == len(first["history"])
    assert second["application"]["status_changed_at"] == (
        first["application"]["status_changed_at"]
    )


async def test_several_portal_sources_still_mean_one_application(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # The same vacancy carried by three portals is one canonical job, so it has
    # one application record - not one per portal.
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    for index in range(3):
        portal = await make_portal(db_session, slug=f"src{index}")
        await make_job_source(db_session, job=job, portal=portal)

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})

    rows = await db_session.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.job_id == job.id)
    )
    assert rows == 1


async def test_a_later_source_does_not_reset_the_status(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # A canonical merge attaches another source to a job already applied to.
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})

    late_portal = await make_portal(db_session, slug="late")
    await make_job_source(db_session, job=job, portal=late_portal)

    data = (await api_client.get(url(job.id, resume.id))).json()["data"]
    assert data["status"] == "applied"


# --- Isolation --------------------------------------------------------------


async def test_two_resumes_track_the_same_job_independently(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    first = await tracked_resume(db_session, user=user)
    second = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(url(job.id, first.id), json={"status": "offer"})

    other = (await api_client.get(url(job.id, second.id))).json()["data"]

    assert other["status"] == "not_applied"
    assert other["application"] is None


async def test_two_jobs_track_independently_under_one_resume(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    applied = await make_job(db_session)
    untouched = await make_job(db_session)

    await api_client.patch(url(applied.id, resume.id), json={"status": "applied"})

    other = (await api_client.get(url(untouched.id, resume.id))).json()["data"]
    assert other["status"] == "not_applied"


async def test_another_users_job_state_is_not_visible(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    stranger = await make_user(db_session)
    their_resume = await tracked_resume(db_session, user=stranger)
    job = await make_job(db_session)

    response = await api_client.get(url(job.id, their_resume.id))

    assert response.status_code == 404


# --- Notes ------------------------------------------------------------------


async def test_a_note_is_kept_when_the_status_changes_without_one(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(
        url(job.id, resume.id),
        json={"status": "applied", "notes": "Applied through referral"},
    )
    # No `notes` key at all: the existing note must survive.
    await api_client.patch(url(job.id, resume.id), json={"status": "interview"})

    data = (await api_client.get(url(job.id, resume.id))).json()["data"]
    assert data["application"]["notes"] == "Applied through referral"
    assert data["status"] == "interview"


async def test_a_note_is_replaced_when_one_is_sent(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(
        url(job.id, resume.id), json={"status": "saved", "notes": "Need follow-up"}
    )
    await api_client.patch(
        url(job.id, resume.id),
        json={"status": "interview", "notes": "Interview scheduled"},
    )

    data = (await api_client.get(url(job.id, resume.id))).json()["data"]
    assert data["application"]["notes"] == "Interview scheduled"


async def test_a_note_can_be_edited_without_changing_the_status(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})
    await api_client.patch(
        url(job.id, resume.id),
        json={"status": "applied", "notes": "Salary discussed"},
    )

    data = (await api_client.get(url(job.id, resume.id))).json()["data"]
    assert data["application"]["notes"] == "Salary discussed"
    assert data["status"] == "applied"
    # A note edit is not a transition, so no history row is added.
    assert len(data["history"]) == 2


async def test_the_notes_indicator_reflects_a_real_note(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    blank = await make_job(db_session)
    noted = await make_job(db_session)

    await api_client.patch(
        url(blank.id, resume.id), json={"status": "saved", "notes": "   "}
    )
    await api_client.patch(
        url(noted.id, resume.id), json={"status": "saved", "notes": "Waiting for HR"}
    )

    blank_data = (await api_client.get(url(blank.id, resume.id))).json()["data"]
    noted_data = (await api_client.get(url(noted.id, resume.id))).json()["data"]

    assert blank_data["application"]["has_notes"] is False
    assert noted_data["application"]["has_notes"] is True


async def test_a_long_note_is_accepted(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # The column is TEXT. An API-side cap would refuse what the database
    # would happily store.
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    long_note = "Interview scheduled. " * 600

    response = await api_client.patch(
        url(job.id, resume.id), json={"status": "interview", "notes": long_note}
    )

    assert response.status_code == 200
    assert response.json()["data"]["notes"] == long_note


# --- Timestamps -------------------------------------------------------------


async def test_status_changed_at_moves_only_on_a_real_change(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    first = (
        await api_client.patch(url(job.id, resume.id), json={"status": "saved"})
    ).json()["data"]

    unchanged = (
        await api_client.patch(url(job.id, resume.id), json={"status": "saved"})
    ).json()["data"]

    moved = (
        await api_client.patch(url(job.id, resume.id), json={"status": "applied"})
    ).json()["data"]

    assert unchanged["status_changed_at"] == first["status_changed_at"]
    assert moved["status_changed_at"] > first["status_changed_at"]


async def test_the_creation_timestamp_is_never_overwritten(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    created = (
        await api_client.patch(url(job.id, resume.id), json={"status": "saved"})
    ).json()["data"]["created_at"]

    for status in ("applied", "interview", "offer"):
        latest = (
            await api_client.patch(url(job.id, resume.id), json={"status": status})
        ).json()["data"]
        assert latest["created_at"] == created


async def test_status_changed_at_matches_the_newest_history_row(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # The value is derived, so it cannot drift from the history it summarises.
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(url(job.id, resume.id), json={"status": "saved"})
    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})

    data = (await api_client.get(url(job.id, resume.id))).json()["data"]
    newest = max(entry["changed_at"] for entry in data["history"])

    assert data["application"]["status_changed_at"] == newest


# --- History ----------------------------------------------------------------


async def test_history_names_the_resume_and_the_canonical_job(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})

    items = (
        await api_client.get(HISTORY, params={"resume_id": str(resume.id)})
    ).json()["data"]["items"]

    assert items
    assert all(entry["resume_id"] == str(resume.id) for entry in items)
    assert all(entry["job_id"] == str(job.id) for entry in items)


async def test_history_can_be_narrowed_to_one_job(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    wanted = await make_job(db_session)
    other = await make_job(db_session)

    await api_client.patch(url(wanted.id, resume.id), json={"status": "applied"})
    await api_client.patch(url(other.id, resume.id), json={"status": "saved"})

    items = (
        await api_client.get(
            HISTORY, params={"resume_id": str(resume.id), "job_id": str(wanted.id)}
        )
    ).json()["data"]["items"]

    assert items
    assert {entry["job_id"] for entry in items} == {str(wanted.id)}


async def test_history_exposes_no_raw_job_fields(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # Raw captures are an audit trail of what a portal returned, not something
    # the user applied to.
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})
    body = (
        await api_client.get(HISTORY, params={"resume_id": str(resume.id)})
    ).text

    assert "raw_payload" not in body
    assert "raw_job" not in body


async def test_earlier_transitions_survive_later_ones(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    for status in ("saved", "applied", "interview", "rejected"):
        await api_client.patch(url(job.id, resume.id), json={"status": status})

    data = (await api_client.get(url(job.id, resume.id))).json()["data"]
    transitions = [
        (entry["old_status"], entry["new_status"]) for entry in data["history"]
    ]

    assert (None, "not_applied") in transitions
    assert ("not_applied", "saved") in transitions
    assert ("saved", "applied") in transitions
    assert ("applied", "interview") in transitions
    assert ("interview", "rejected") in transitions


async def test_history_rows_are_never_edited(
    db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await application_service.set_status(
        db_session,
        user_id=user.id,
        resume_id=resume.id,
        job_id=job.id,
        status=ApplicationStatus.APPLIED,
    )
    await db_session.flush()

    first = list(
        (
            await db_session.scalars(
                select(ApplicationStatusHistory).order_by(
                    ApplicationStatusHistory.changed_at
                )
            )
        ).all()
    )
    snapshot = [(row.id, row.old_status, row.new_status) for row in first]

    await application_service.set_status(
        db_session,
        user_id=user.id,
        resume_id=resume.id,
        job_id=job.id,
        status=ApplicationStatus.OFFER,
    )
    await db_session.flush()

    after = list(
        (
            await db_session.scalars(
                select(ApplicationStatusHistory).order_by(
                    ApplicationStatusHistory.changed_at
                )
            )
        ).all()
    )

    assert [(row.id, row.old_status, row.new_status) for row in after[: len(snapshot)]] == (
        snapshot
    )


# --- Rejections -------------------------------------------------------------


@pytest.mark.parametrize("value", ["expired", "APPLIED_MAYBE", "", "pending"])
async def test_an_unknown_status_is_rejected(
    api_client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    value: str,
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    response = await api_client.patch(url(job.id, resume.id), json={"status": value})

    assert response.status_code == 422
    assert response.json()["success"] is False


async def test_an_unknown_job_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)

    patched = await api_client.patch(
        url(uuid.uuid4(), resume.id), json={"status": "saved"}
    )
    fetched = await api_client.get(url(uuid.uuid4(), resume.id))

    assert patched.status_code == 404
    assert fetched.status_code == 404
    assert fetched.json()["errors"][0]["code"] == "job_not_found"


async def test_an_unknown_resume_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    job = await make_job(db_session)

    patched = await api_client.patch(
        url(job.id, uuid.uuid4()), json={"status": "saved"}
    )
    fetched = await api_client.get(url(job.id, uuid.uuid4()))

    assert patched.status_code == 404
    assert fetched.status_code == 404
    assert fetched.json()["errors"][0]["code"] == "resume_not_found"


async def test_the_resume_id_is_required(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Without it the request does not identify whose history it is changing.
    job = await make_job(db_session)

    response = await api_client.patch(
        f"{APPS}/{job.id}", json={"status": "saved"}
    )

    assert response.status_code == 422


# --- Search history stays read-only ----------------------------------------


async def test_a_status_change_does_not_touch_the_match_score(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)
    match = await make_match(db_session, resume=resume, job=job, final_score=91.5)

    before = (match.final_score, match.ranking_score, match.qualified)

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})
    await db_session.refresh(match)

    assert (match.final_score, match.ranking_score, match.qualified) == before


async def test_a_status_change_does_not_rewrite_a_run_summary(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    adapter = build_fake_portal(
        "track-run", jobs=[make_raw_job(portal="track-run", index=0, now=NOW)]
    )
    portal = await make_portal(
        db_session, slug="track-run", adapter_key=adapter.slug, options={}
    )

    prepared = await search_run_service.create_search_run(
        db_session,
        resume=resume,
        portal_ids=[portal.id],
        requested_limit=20,
        require_adapter=False,
    )
    outcome = await execute_search_run(
        db_session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters={portal.id: adapter()},
        now=NOW,
    )
    await db_session.commit()

    summary_before = dict(prepared.run.summary)
    leg_before = (
        prepared.legs[0].qualified_count,
        prepared.legs[0].selected_count,
        prepared.legs[0].raw_count,
    )
    job_id = outcome.results[0].job_id

    await api_client.patch(url(job_id, resume.id), json={"status": "applied"})

    run = await db_session.scalar(
        select(SearchRun).where(SearchRun.id == prepared.run.id)
    )
    await db_session.refresh(prepared.legs[0])

    assert dict(run.summary) == summary_before
    assert (
        prepared.legs[0].qualified_count,
        prepared.legs[0].selected_count,
        prepared.legs[0].raw_count,
    ) == leg_before


# --- Search integration -----------------------------------------------------


async def build_run(db_session: AsyncSession, *, resume, slug: str, listing):
    adapter = build_fake_portal(slug, jobs=[listing])
    portal = await make_portal(
        db_session, slug=slug, adapter_key=adapter.slug, options={}
    )
    prepared = await search_run_service.create_search_run(
        db_session,
        resume=resume,
        portal_ids=[portal.id],
        requested_limit=20,
        require_adapter=False,
    )
    return await execute_search_run(
        db_session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters={portal.id: adapter()},
        now=NOW,
    )


@pytest.mark.parametrize("status", ["applied", "skipped"])
async def test_a_decided_status_removes_the_job_from_the_next_search(
    api_client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    status: str,
) -> None:
    """Applied and Skipped are both decisions, so neither is offered again.

    The whole point of Skipped: the user looked at this job and passed on it.
    Returning it in the next run would waste the slot on something already
    rejected.
    """
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    listing = make_raw_job(portal="flow", index=0, company="Flow Co", now=NOW)

    first = await build_run(
        db_session, resume=resume, slug=f"flow1-{status}", listing=listing
    )
    await db_session.commit()
    assert first.returned_count == 1
    job_id = first.results[0].job_id

    await api_client.patch(url(job_id, resume.id), json={"status": status})

    second = await build_run(
        db_session, resume=resume, slug=f"flow2-{status}", listing=listing
    )
    assert second.returned_count == 0


@pytest.mark.parametrize("status", ["saved", "not_applied"])
async def test_an_eligible_status_keeps_the_job_in_the_next_search(
    api_client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    status: str,
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    listing = make_raw_job(portal="keep", index=0, company="Keep Co", now=NOW)

    first = await build_run(db_session, resume=resume, slug="keep1", listing=listing)
    await db_session.commit()
    job_id = first.results[0].job_id

    await api_client.patch(url(job_id, resume.id), json={"status": status})

    second = await build_run(db_session, resume=resume, slug="keep2", listing=listing)

    assert second.returned_count == 1
    assert second.results[0].job_id == job_id
    assert second.results[0].application_status.value == status


@pytest.mark.parametrize("status", ["applied", "skipped"])
async def test_one_resume_deciding_does_not_hide_the_job_from_another(
    api_client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    status: str,
) -> None:
    # Resume A decided, resume B never did: the job must still appear for B.
    # Skipping a job as a Financial Analyst says nothing about whether it suits
    # the user's other resume.
    user = await acting_user(db_session, settings)
    first_resume = await tracked_resume(db_session, user=user)
    second_resume = await tracked_resume(db_session, user=user)
    listing = make_raw_job(portal="two", index=0, company="Two Co", now=NOW)

    first = await build_run(
        db_session, resume=first_resume, slug=f"two1-{status}", listing=listing
    )
    await db_session.commit()
    job_id = first.results[0].job_id

    await api_client.patch(url(job_id, first_resume.id), json={"status": status})

    other = await build_run(
        db_session, resume=second_resume, slug=f"two2-{status}", listing=listing
    )

    assert other.returned_count == 1
    assert other.results[0].job_id == job_id
    assert other.results[0].application_status is ApplicationStatus.NOT_APPLIED


async def test_the_search_result_shows_the_current_status(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    listing = make_raw_job(portal="show", index=0, company="Show Co", now=NOW)

    first = await build_run(db_session, resume=resume, slug="show1", listing=listing)
    await db_session.commit()
    job_id = first.results[0].job_id

    await api_client.patch(url(job_id, resume.id), json={"status": "saved"})

    second = await build_run(db_session, resume=resume, slug="show2", listing=listing)

    assert second.results[0].application_status is ApplicationStatus.SAVED


async def test_an_applied_jobs_history_survives_later_searches(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    listing = make_raw_job(portal="hist", index=0, company="Hist Co", now=NOW)

    first = await build_run(db_session, resume=resume, slug="hist1", listing=listing)
    await db_session.commit()
    job_id = first.results[0].job_id

    await api_client.patch(
        url(job_id, resume.id), json={"status": "applied", "notes": "Round one"}
    )
    before = (await api_client.get(url(job_id, resume.id))).json()["data"]

    await build_run(db_session, resume=resume, slug="hist2", listing=listing)

    after = (await api_client.get(url(job_id, resume.id))).json()["data"]

    assert after["status"] == "applied"
    assert after["application"]["notes"] == "Round one"
    assert len(after["history"]) == len(before["history"])
    assert after["application"]["applied_at"] == before["application"]["applied_at"]


# --- Dashboard contract -----------------------------------------------------


async def test_the_status_payload_carries_what_the_dashboard_renders(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    # frontend/src/components/ApplicationStatus.jsx reads exactly these.
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    payload = (
        await api_client.patch(
            url(job.id, resume.id), json={"status": "saved", "notes": "Follow up"}
        )
    ).json()["data"]

    for field in (
        "status",
        "notes",
        "has_notes",
        "status_changed_at",
        "updated_at",
        "created_at",
    ):
        assert field in payload, field


async def test_every_status_in_the_vocabulary_is_accepted(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """The API accepts the whole vocabulary, not only what the dropdown shows.

    Find Matches offers three of them (see the dropdown test below), but rows
    recorded under any status - by an earlier build, by the history page, or by
    a direct PATCH - must still be writable and readable.
    """
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    accepted = [
        "not_applied",
        "saved",
        "applied",
        "interview",
        "rejected",
        "offer",
        "skipped",
    ]
    assert accepted == [status.value for status in ApplicationStatus]

    for status in accepted:
        response = await api_client.patch(
            url(job.id, resume.id), json={"status": status}
        )
        assert response.status_code == 200, (status, response.text)
        assert response.json()["data"]["status"] == status


def test_the_find_matches_dropdown_offers_exactly_three_statuses() -> None:
    """The dropdown is trimmed to the three decisions the row supports.

    Read from the source rather than asserted in prose: there is no JavaScript
    test runner in this project, and a comment cannot fail when the list drifts.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "components"
        / "ApplicationStatus.jsx"
    ).read_text(encoding="utf-8")

    match = re.search(r"export const STATUSES = \[(.*?)\]", source, re.DOTALL)
    assert match is not None, "STATUSES is no longer declared as an array literal."

    offered = re.findall(r"'([a-z_]+)'", match.group(1))

    assert offered == ["not_applied", "applied", "skipped"]

    # Both excluding statuses must be marked as such, or the tooltip would tell
    # the user a skipped job is still eligible.
    excluding = re.search(r"const EXCLUDING = new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert excluding is not None
    assert "'skipped'" in excluding.group(1)
    assert "'applied'" in excluding.group(1)


def test_application_history_filters_to_all_applied_and_skipped() -> None:
    """The history page offers three filters and one summary card.

    Only what is *shown* is trimmed. ``ALL_TRACKED`` stays complete in the same
    file, because the table still lists rows recorded under any status and the
    board column is enriched per status - a row saved as Interview must not
    vanish just because it has no filter tab of its own.
    """
    source = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "pages"
        / "ApplicationHistory.jsx"
    ).read_text(encoding="utf-8")

    filters = re.search(r"const FILTERS = \[(.*?)\]", source, re.DOTALL)
    assert filters is not None, "FILTERS is no longer declared as an array literal."
    assert re.findall(r"'([a-z_]+)'", filters.group(1)) == ["applied", "skipped"]

    # Rendered alongside a hardcoded All pill, and nothing else.
    assert source.count("<FilterPill") == 2
    assert "{FILTERS.map((status)" in source

    tracked = re.search(r"const ALL_TRACKED = \[(.*?)\]", source, re.DOTALL)
    assert tracked is not None
    for status in ("interview", "offer", "rejected", "saved"):
        assert status in tracked.group(0) or status in "".join(
            re.findall(r"const OUTCOMES = \[(.*?)\]", source, re.DOTALL)
        ), status

    # One summary card: Applications. The other three are gone.
    assert source.count("<StatCard") == 1
    assert 'label="Applications"' in source
    for removed in ('label="Interviews"', 'label="Offers"', 'label="Rejected"'):
        assert removed not in source, removed


async def test_the_applications_list_carries_the_derived_timestamp(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(db_session)

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})

    items = (
        await api_client.get(APPS, params={"resume_id": str(resume.id)})
    ).json()["data"]["items"]

    assert len(items) == 1
    assert items[0]["status_changed_at"]
    assert items[0]["job_title"]


async def test_no_auto_apply_concept_appears_in_the_tracking_payloads(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user = await acting_user(db_session, settings)
    resume = await tracked_resume(db_session, user=user)
    job = await make_job(
        db_session, preferred_application_url="https://acme.invalid/apply"
    )

    await api_client.patch(url(job.id, resume.id), json={"status": "applied"})
    body = (await api_client.get(url(job.id, resume.id))).text.lower()

    for banned in ("easyapply", "easy_apply", "autosubmit", "auto_apply", "formprogress"):
        assert banned not in body.replace(" ", ""), banned
