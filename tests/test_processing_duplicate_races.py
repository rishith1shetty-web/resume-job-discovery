"""Duplicate handling must be idempotent, including under a concurrent insert.

``jobs.canonical_key`` is unique, and until this was fixed a second writer
reaching that key first raised ``UniqueViolationError`` out of
``process_raw_jobs``. That killed the whole portal leg: the orchestrator's
leg-level savepoint rolled back, so every listing the portal had already stored
was discarded and the portal reported zero.

The constraint stays. What changed is that losing the race is now handled where
it happens - the losing writer adopts the row that won.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job, JobSource
from app.portals.base import RawJob
from app.services import job_processing
from app.services.job_processing import process_raw_jobs
from tests.factories import make_portal

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
DESCRIPTION = (
    "Own the monthly close, build the operating model and partner with the "
    "business on forecasting and budgeting. " * 8
)


def listing(
    *,
    external_id: str = "ext-1",
    source: str = "jobright",
    url: str = "https://jobright.ai/jobs/info/abc123",
    title: str = "Senior Financial Analyst",
    company: str | None = "Contoso Ltd",
    location: str = "Austin, TX, USA",
    age_days: float = 1.0,
) -> RawJob:
    return RawJob(
        external_id=external_id,
        source=source,
        title=title,
        company=company,
        location=location,
        description=DESCRIPTION,
        source_url=url,
        application_url=url,
        posted_at=NOW - timedelta(days=age_days),
        employment_type="Full-time",
        raw_payload={},
    )


async def count_jobs(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Job)) or 0


async def count_sources(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(JobSource)) or 0


# --- The ordinary idempotence guarantees ------------------------------------


async def test_the_same_job_encountered_twice_creates_one_job(
    db_session: AsyncSession,
) -> None:
    portal = await make_portal(db_session, slug="dup-a", is_implemented=True)

    first = await process_raw_jobs(
        db_session, portal=portal, raw_jobs=[listing()], now=NOW
    )
    second = await process_raw_jobs(
        db_session, portal=portal, raw_jobs=[listing()], now=NOW
    )

    assert first.jobs_created == 1
    assert second.jobs_created == 0
    assert await count_jobs(db_session) == 1
    # And one source row, not two: the portal already had one.
    assert await count_sources(db_session) == 1


async def test_the_same_job_twice_in_one_batch_creates_one_job(
    db_session: AsyncSession,
) -> None:
    """Repeated portal results inside a single response."""
    portal = await make_portal(db_session, slug="dup-b", is_implemented=True)

    result = await process_raw_jobs(
        db_session, portal=portal, raw_jobs=[listing(), listing()], now=NOW
    )

    assert result.jobs_created == 1
    assert await count_jobs(db_session) == 1


async def test_the_same_canonical_url_encountered_twice_creates_one_job(
    db_session: AsyncSession,
) -> None:
    """Same vacancy, same URL, different tracking parameters and a new id."""
    portal = await make_portal(db_session, slug="dup-c", is_implemented=True)

    await process_raw_jobs(
        db_session,
        portal=portal,
        raw_jobs=[listing(url="https://jobright.ai/jobs/info/abc123?utm_source=1")],
        now=NOW,
    )
    await process_raw_jobs(
        db_session,
        portal=portal,
        raw_jobs=[
            listing(
                external_id="ext-2",
                url="https://jobright.ai/jobs/info/abc123?utm_source=999",
            )
        ],
        now=NOW,
    )

    assert await count_jobs(db_session) == 1


async def test_a_job_found_by_another_portal_joins_the_existing_job(
    db_session: AsyncSession,
) -> None:
    """One canonical job, both portals' sources preserved (CLAUDE.md section 9)."""
    first_portal = await make_portal(db_session, slug="dup-d1", is_implemented=True)
    second_portal = await make_portal(db_session, slug="dup-d2", is_implemented=True)

    await process_raw_jobs(
        db_session, portal=first_portal, raw_jobs=[listing()], now=NOW
    )
    second = await process_raw_jobs(
        db_session,
        portal=second_portal,
        raw_jobs=[
            listing(
                external_id="other-1",
                source="hiringcafe",
                url="https://hiringcafe.com/jobs/senior-financial-analyst",
            )
        ],
        now=NOW,
    )

    assert second.jobs_created == 0
    assert await count_jobs(db_session) == 1
    # Both portals keep their own listing row.
    assert await count_sources(db_session) == 2


# --- The race ----------------------------------------------------------------


@pytest.fixture
def blind_candidate_lookup(monkeypatch: pytest.MonkeyPatch):
    """Make the first candidate lookup miss what is already stored.

    That is precisely what a concurrent writer causes: this session looks for
    duplicates, finds none, and by the time it flushes another transaction has
    committed the same canonical key. Simulated rather than run on two real
    connections because the test session is a single rolled-back transaction -
    a second connection could not see its rows, and would leave its own behind.
    """
    real = job_processing._load_candidates
    state = {"blind": True}

    async def _blinded(session, candidate):  # noqa: ANN001
        if state["blind"]:
            state["blind"] = False
            return []
        return await real(session, candidate)

    monkeypatch.setattr(job_processing, "_load_candidates", _blinded)
    return state


async def test_losing_the_canonical_key_race_adopts_the_winning_job(
    db_session: AsyncSession, blind_candidate_lookup
) -> None:
    """The regression: this used to raise UniqueViolationError."""
    portal = await make_portal(db_session, slug="race-a", is_implemented=True)

    # The winner, already stored.
    await process_raw_jobs(
        db_session, portal=portal, raw_jobs=[listing()], now=NOW
    )
    assert await count_jobs(db_session) == 1

    # The loser: its candidate lookup sees nothing, so it tries to insert.
    result = await process_raw_jobs(
        db_session,
        portal=portal,
        raw_jobs=[listing(external_id="ext-racer")],
        now=NOW,
    )

    assert result.jobs_created == 0, "the loser must adopt, not create"
    assert await count_jobs(db_session) == 1
    assert result.processed[0].job_id is not None


async def test_the_race_does_not_terminate_the_batch(
    db_session: AsyncSession, blind_candidate_lookup
) -> None:
    """A duplicate must not cost the portal the rest of its listings."""
    portal = await make_portal(db_session, slug="race-b", is_implemented=True)

    await process_raw_jobs(
        db_session, portal=portal, raw_jobs=[listing()], now=NOW
    )

    result = await process_raw_jobs(
        db_session,
        portal=portal,
        raw_jobs=[
            listing(external_id="ext-racer"),
            listing(
                external_id="ext-other",
                title="Financial Planning Analyst",
                url="https://jobright.ai/jobs/info/second",
            ),
        ],
        now=NOW,
    )

    # Both listings were processed; the second is a genuinely new job.
    assert len(result.processed) == 2
    assert await count_jobs(db_session) == 2


async def test_the_race_still_records_this_portals_source(
    db_session: AsyncSession, blind_candidate_lookup
) -> None:
    """Adopting must not lose the losing writer's listing."""
    first_portal = await make_portal(db_session, slug="race-c1", is_implemented=True)
    second_portal = await make_portal(db_session, slug="race-c2", is_implemented=True)

    await process_raw_jobs(
        db_session, portal=first_portal, raw_jobs=[listing()], now=NOW
    )
    await process_raw_jobs(
        db_session,
        portal=second_portal,
        raw_jobs=[
            listing(
                external_id="other-1",
                source="hiringcafe",
                url="https://hiringcafe.com/jobs/senior-financial-analyst",
            )
        ],
        now=NOW,
    )

    assert await count_jobs(db_session) == 1
    assert await count_sources(db_session) == 2


async def test_an_unrelated_integrity_error_is_not_swallowed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the canonical-key conflict is retried.

    A foreign-key or not-null violation means something is genuinely wrong, and
    quietly adopting some other row would hide it.
    """
    from sqlalchemy.exc import IntegrityError

    portal = await make_portal(db_session, slug="race-d", is_implemented=True)

    async def _boom(session, job):  # noqa: ANN001
        raise IntegrityError(
            "INSERT ...", {}, Exception('violates foreign key constraint "fk_jobs_x"')
        )

    monkeypatch.setattr(job_processing, "_insert_job", _boom)

    with pytest.raises(IntegrityError):
        await process_raw_jobs(
            db_session, portal=portal, raw_jobs=[listing()], now=NOW
        )


def test_the_conflict_test_only_matches_the_canonical_key_constraint() -> None:
    from sqlalchemy.exc import IntegrityError

    canonical = IntegrityError(
        "INSERT", {}, Exception('duplicate key value violates unique constraint "uq_jobs_canonical_key"')
    )
    unrelated = IntegrityError(
        "INSERT", {}, Exception('duplicate key value violates unique constraint "uq_portals_slug"')
    )

    assert job_processing._is_canonical_key_conflict(canonical) is True
    assert job_processing._is_canonical_key_conflict(unrelated) is False
