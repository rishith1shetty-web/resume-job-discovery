"""Storing canonical jobs, preserving every source, and staying idempotent.

Integration tests. Each runs inside a transaction that is always rolled back,
so nothing reaches ``resume_job_discovery`` permanently, and every record
created carries the shared test marker.

Cross-portal cases use constructed ``RawJob`` records. LinkedIn live collection
is disabled, so no test here contacts a portal - a second portal is simulated
by giving a record a different ``source``, and that is a fixture, not evidence
of live two-portal collection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import ApplicationStatus, JobUrlKind, WorkMode
from app.models.application import Application, ApplicationStatusHistory
from app.models.job import Job, JobSource
from app.models.portal import Portal
from app.portals.base import RawJob
from app.services.job_processing import preview_raw_jobs, process_raw_jobs
from tests.conftest import TEST_RECORD_MARKER
from tests.factories import make_portal, make_resume, make_user

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture
async def portals(db_session: AsyncSession) -> tuple[Portal, Portal]:
    """Two throwaway portals, marked so they are obviously not configuration.

    Deliberately not the real jobright and linkedin rows: those are committed
    configuration, and a test must not depend on whether an operator has seeded
    them or change anything about them.
    """
    alpha = await make_portal(db_session, slug="alpha", is_implemented=True)
    beta = await make_portal(db_session, slug="beta", is_implemented=True)
    return alpha, beta


def make_raw(
    *,
    source: str,
    external_id: str | None = "x-1",
    title: str = "Financial Analyst",
    company: str | None = "Contoso",
    location: str | None = "Boston, MA",
    description: str | None = None,
    source_url: str | None = None,
    application_url: str | None = None,
    posted_at: datetime | None = None,
    employment_type: str | None = "Full-time",
) -> RawJob:
    listing = source_url or f"https://example.invalid/{source}/{external_id}"
    return RawJob(
        external_id=external_id,
        source=source,
        title=title,
        company=company,
        location=location,
        description=description,
        source_url=listing,
        application_url=application_url or listing,
        posted_at=posted_at if posted_at is not None else NOW - timedelta(days=1),
        employment_type=employment_type,
        raw_payload={"marker": TEST_RECORD_MARKER},
    )


async def load_job(session: AsyncSession, job_id) -> Job:
    job = await session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .options(selectinload(Job.sources).selectinload(JobSource.portal))
    )
    assert job is not None
    return job


# --- Canonical job creation -------------------------------------------------


async def test_a_listing_becomes_a_canonical_job_with_one_source(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[make_raw(source=alpha.slug)],
        now=NOW,
    )

    assert result.jobs_created == 1
    assert result.sources_created == 1

    job = await load_job(db_session, result.processed[0].job_id)
    assert job.title == "Financial Analyst"
    assert job.company == "Contoso"
    assert job.normalized_company == "contoso"
    assert job.location == "Boston, MA"
    assert job.first_discovered_at == NOW
    assert len(job.sources) == 1


async def test_normalised_fields_are_stored_alongside_display_values(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(
                source=alpha.slug,
                title="Sr. Financial Analyst (Remote) - Req #48210",
                company="Contoso Ltd",
                description="d" * 500,
            )
        ],
        now=NOW,
    )

    job = await load_job(db_session, result.processed[0].job_id)

    # Display keeps the employer's wording; the keys are the flattened forms.
    assert job.title == "Sr. Financial Analyst (Remote)"
    assert job.normalized_title == "senior financial analyst"
    assert job.company == "Contoso Ltd"
    assert job.normalized_company == "contoso"
    assert job.work_mode is WorkMode.REMOTE
    assert job.description_fingerprint is not None


async def test_a_listing_without_a_usable_url_is_rejected_not_stored(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[make_raw(source=alpha.slug, source_url="javascript:void(0)")],
        now=NOW,
    )

    assert result.processed == []
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "no_valid_source_url"


async def test_one_unusable_listing_does_not_stop_the_rest_of_the_batch(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    # CLAUDE.md section 18: a failure is isolated, never allowed to cascade.
    alpha, _ = portals

    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(source=alpha.slug, external_id="good-1"),
            make_raw(source=alpha.slug, external_id="bad", title="   "),
            make_raw(
                source=alpha.slug, external_id="good-2", company="Fabrikam"
            ),
        ],
        now=NOW,
    )

    assert len(result.processed) == 2
    assert len(result.rejected) == 1
    assert result.rejected[0].reason == "no_usable_title"


# --- Source preservation ----------------------------------------------------


async def test_the_same_job_from_two_portals_keeps_both_sources(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, beta = portals

    first = await process_raw_jobs(
        db_session, portal=alpha, raw_jobs=[make_raw(source=alpha.slug)], now=NOW
    )
    second = await process_raw_jobs(
        db_session, portal=beta, raw_jobs=[make_raw(source=beta.slug)], now=NOW
    )

    assert second.jobs_created == 0, "the second portal created a duplicate job"
    assert second.duplicates == 1
    assert first.processed[0].job_id == second.processed[0].job_id

    job = await load_job(db_session, first.processed[0].job_id)
    assert {source.portal.slug for source in job.sources} == {alpha.slug, beta.slug}


async def test_each_source_keeps_its_own_url_and_external_id(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    # Choosing one preferred URL must never discard another portal's link.
    alpha, beta = portals

    await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[make_raw(source=alpha.slug, external_id="alpha-1")],
        now=NOW,
    )
    result = await process_raw_jobs(
        db_session,
        portal=beta,
        raw_jobs=[make_raw(source=beta.slug, external_id="beta-1")],
        now=NOW,
    )

    job = await load_job(db_session, result.processed[0].job_id)
    by_slug = {source.portal.slug: source for source in job.sources}

    assert by_slug[alpha.slug].external_id == "alpha-1"
    assert by_slug[beta.slug].external_id == "beta-1"
    assert by_slug[alpha.slug].source_url != by_slug[beta.slug].source_url


async def test_rediscovery_updates_the_existing_source_rather_than_adding_one(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals
    later = NOW + timedelta(hours=6)

    await process_raw_jobs(
        db_session, portal=alpha, raw_jobs=[make_raw(source=alpha.slug)], now=NOW
    )
    result = await process_raw_jobs(
        db_session, portal=alpha, raw_jobs=[make_raw(source=alpha.slug)], now=later
    )

    job = await load_job(db_session, result.processed[0].job_id)

    assert len(job.sources) == 1
    assert job.sources[0].last_seen_at == later
    assert job.sources[0].first_seen_at == NOW


# --- Preferred URL ----------------------------------------------------------


async def test_the_most_direct_url_across_sources_is_preferred(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, beta = portals
    ats = "https://boards.greenhouse.io/contoso/jobs/4123456?gh_jid=4123456"

    await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[make_raw(source=alpha.slug, external_id="a-1")],
        now=NOW,
    )
    result = await process_raw_jobs(
        db_session,
        portal=beta,
        raw_jobs=[make_raw(source=beta.slug, external_id="b-1", application_url=ats)],
        now=NOW,
    )

    job = await load_job(db_session, result.processed[0].job_id)

    assert job.preferred_url_kind is JobUrlKind.ATS
    assert job.preferred_application_url == ats
    # And the portal that supplied it is flagged, without losing the other.
    preferred = [source for source in job.sources if source.is_preferred]
    assert len(preferred) == 1
    assert preferred[0].portal_id == beta.id
    assert len(job.sources) == 2


async def test_a_portal_listing_is_a_usable_preferred_url(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    # Jobright exposes no employer-direct link, so its own job page is what the
    # user opens. Every job must give the user something to open.
    alpha, _ = portals

    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(
                source=alpha.slug,
                source_url="https://jobright.ai/jobs/info/abc?utm_source=1014",
            )
        ],
        now=NOW,
    )

    job = await load_job(db_session, result.processed[0].job_id)

    assert job.preferred_application_url == "https://jobright.ai/jobs/info/abc"
    assert job.preferred_application_url.startswith("https://")


async def test_the_preferred_url_does_not_change_between_identical_runs(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, beta = portals
    batch_alpha = [make_raw(source=alpha.slug, external_id="a-1")]
    batch_beta = [make_raw(source=beta.slug, external_id="b-1")]

    await process_raw_jobs(db_session, portal=alpha, raw_jobs=batch_alpha, now=NOW)
    first = await process_raw_jobs(db_session, portal=beta, raw_jobs=batch_beta, now=NOW)
    job = await load_job(db_session, first.processed[0].job_id)
    chosen = job.preferred_application_url

    await process_raw_jobs(db_session, portal=beta, raw_jobs=batch_beta, now=NOW)
    await process_raw_jobs(db_session, portal=alpha, raw_jobs=batch_alpha, now=NOW)
    job = await load_job(db_session, first.processed[0].job_id)

    assert job.preferred_application_url == chosen


# --- Deduplication in the database -----------------------------------------


async def test_the_same_external_id_on_one_portal_never_creates_a_second_job(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[make_raw(source=alpha.slug, external_id="dup-1")],
        now=NOW,
    )
    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(
                source=alpha.slug,
                external_id="dup-1",
                # Even with the metadata since changed, the portal's own id wins.
                title="Financial Analyst II",
                location="Denver, CO",
            )
        ],
        now=NOW,
    )

    assert result.jobs_created == 0
    total = await db_session.scalar(select(func.count()).select_from(Job))
    assert total == 1


async def test_different_employers_produce_separate_jobs(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(source=alpha.slug, external_id="c-1", company="Contoso"),
            make_raw(source=alpha.slug, external_id="f-1", company="Fabrikam"),
        ],
        now=NOW,
    )

    assert result.jobs_created == 2
    assert len(result.canonical_job_ids) == 2


async def test_conflicting_locations_produce_separate_jobs(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(source=alpha.slug, external_id="b-1", location="Boston, MA"),
            make_raw(source=alpha.slug, external_id="d-1", location="Denver, CO"),
        ],
        now=NOW,
    )

    assert result.jobs_created == 2


async def test_different_seniority_produces_separate_jobs(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    result = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(
                source=alpha.slug, external_id="s-1", title="Senior Financial Analyst"
            ),
            make_raw(
                source=alpha.slug, external_id="j-1", title="Junior Financial Analyst"
            ),
        ],
        now=NOW,
    )

    assert result.jobs_created == 2


async def test_a_shared_ats_url_merges_across_portals_despite_different_wording(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, beta = portals
    ats = "https://boards.greenhouse.io/contoso/jobs/77?gh_jid=77"

    first = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[make_raw(source=alpha.slug, external_id="a-1", application_url=ats)],
        now=NOW,
    )
    second = await process_raw_jobs(
        db_session,
        portal=beta,
        raw_jobs=[
            make_raw(
                source=beta.slug,
                external_id="b-1",
                application_url=f"{ats}&gh_src=tracking",
                # A different employer spelling and a vaguer location: only the
                # shared requisition URL ties these together.
                company="Contoso Corporation",
                location="Boston",
            )
        ],
        now=NOW,
    )

    assert second.jobs_created == 0
    assert first.processed[0].job_id == second.processed[0].job_id


# --- Merging quality --------------------------------------------------------


async def test_a_second_source_improves_fields_without_degrading_them(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, beta = portals
    full_description = "A complete posting. " * 200

    await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(
                source=alpha.slug,
                external_id="a-1",
                title="FINANCIAL ANALYST",
                location="Boston",
                description="A short teaser.",
                employment_type=None,
                posted_at=NOW,
            )
        ],
        now=NOW,
    )
    result = await process_raw_jobs(
        db_session,
        portal=beta,
        raw_jobs=[
            make_raw(
                source=beta.slug,
                external_id="b-1",
                title="Financial Analyst",
                location="Boston, MA",
                description=full_description,
                employment_type="Full-time",
                posted_at=NOW - timedelta(days=3),
            )
        ],
        now=NOW,
    )

    job = await load_job(db_session, result.processed[0].job_id)

    assert job.title == "Financial Analyst", "a readable title should win"
    assert job.location == "Boston, MA", "the more specific location should win"
    assert job.description == full_description.strip()
    assert job.employment_type == "Full-time"
    # The earliest known date is closest to when the employer published it.
    assert job.posted_at == NOW - timedelta(days=3)
    # Comparison keys follow their display values.
    assert job.normalized_title == "financial analyst"


async def test_a_worse_second_source_changes_nothing(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, beta = portals
    full_description = "A complete posting. " * 200

    first = await process_raw_jobs(
        db_session,
        portal=alpha,
        raw_jobs=[
            make_raw(
                source=alpha.slug,
                external_id="a-1",
                description=full_description,
                location="Boston, MA",
            )
        ],
        now=NOW,
    )
    result = await process_raw_jobs(
        db_session,
        portal=beta,
        raw_jobs=[
            make_raw(
                source=beta.slug,
                external_id="b-1",
                description="tiny",
                location="Boston",
            )
        ],
        now=NOW,
    )

    assert result.processed[0].changed_fields == ()
    job = await load_job(db_session, first.processed[0].job_id)
    assert job.description == full_description.strip()
    assert job.location == "Boston, MA"


# --- Idempotency ------------------------------------------------------------


async def test_processing_the_same_batch_twice_creates_nothing_new(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals
    batch = [
        make_raw(source=alpha.slug, external_id="i-1"),
        make_raw(source=alpha.slug, external_id="i-2", company="Fabrikam"),
        make_raw(source=alpha.slug, external_id="i-3", title="Data Engineer"),
    ]

    first = await process_raw_jobs(db_session, portal=alpha, raw_jobs=batch, now=NOW)
    jobs_after_first = await db_session.scalar(select(func.count()).select_from(Job))
    sources_after_first = await db_session.scalar(
        select(func.count()).select_from(JobSource)
    )

    second = await process_raw_jobs(db_session, portal=alpha, raw_jobs=batch, now=NOW)

    assert second.jobs_created == 0
    assert second.sources_created == 0
    assert await db_session.scalar(select(func.count()).select_from(Job)) == (
        jobs_after_first
    )
    assert await db_session.scalar(select(func.count()).select_from(JobSource)) == (
        sources_after_first
    )
    assert {item.job_id for item in first.processed} == {
        item.job_id for item in second.processed
    }


async def test_reprocessing_does_not_rewrite_stored_fields(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals
    batch = [make_raw(source=alpha.slug, external_id="i-1", description="d" * 900)]

    await process_raw_jobs(db_session, portal=alpha, raw_jobs=batch, now=NOW)
    second = await process_raw_jobs(db_session, portal=alpha, raw_jobs=batch, now=NOW)

    assert second.processed[0].changed_fields == ()


async def test_processing_order_does_not_change_the_result(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals
    batch = [
        make_raw(source=alpha.slug, external_id="o-1"),
        make_raw(source=alpha.slug, external_id="o-2", company="Fabrikam"),
    ]

    await process_raw_jobs(db_session, portal=alpha, raw_jobs=batch, now=NOW)
    forward = await db_session.scalar(select(func.count()).select_from(Job))

    await process_raw_jobs(
        db_session, portal=alpha, raw_jobs=list(reversed(batch)), now=NOW
    )

    assert await db_session.scalar(select(func.count()).select_from(Job)) == forward


# --- Application history ----------------------------------------------------


async def test_merging_a_new_source_preserves_an_existing_application(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    """A job already marked Applied must not come back as a new opportunity.

    CLAUDE.md section 9: duplicate discovery preserves prior application
    status. Status is keyed on (user, resume, canonical job), so merging a
    second portal into the same canonical job leaves it untouched.
    """
    alpha, beta = portals
    user = await make_user(db_session)
    resume = await make_resume(db_session, user=user)

    created = await process_raw_jobs(
        db_session, portal=alpha, raw_jobs=[make_raw(source=alpha.slug)], now=NOW
    )
    job_id = created.processed[0].job_id

    application = Application(
        user_id=user.id,
        resume_id=resume.id,
        job_id=job_id,
        status=ApplicationStatus.APPLIED,
        applied_at=NOW,
        notes=TEST_RECORD_MARKER,
    )
    db_session.add(application)
    db_session.add(
        ApplicationStatusHistory(
            application=application,
            old_status=ApplicationStatus.NOT_APPLIED,
            new_status=ApplicationStatus.APPLIED,
        )
    )
    await db_session.flush()

    # The same vacancy arrives again from another portal.
    merged = await process_raw_jobs(
        db_session, portal=beta, raw_jobs=[make_raw(source=beta.slug)], now=NOW
    )

    assert merged.processed[0].job_id == job_id
    await db_session.refresh(application)
    assert application.status is ApplicationStatus.APPLIED
    assert application.applied_at == NOW

    applications = await db_session.scalar(
        select(func.count()).select_from(Application).where(Application.job_id == job_id)
    )
    history = await db_session.scalar(
        select(func.count())
        .select_from(ApplicationStatusHistory)
        .where(ApplicationStatusHistory.application_id == application.id)
    )
    assert applications == 1, "merging must not create a second application"
    assert history == 1, "merging must not write application history"


async def test_processing_writes_no_application_rows_at_all(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    await process_raw_jobs(
        db_session, portal=alpha, raw_jobs=[make_raw(source=alpha.slug)], now=NOW
    )

    assert await db_session.scalar(select(func.count()).select_from(Application)) == 0
    assert (
        await db_session.scalar(
            select(func.count()).select_from(ApplicationStatusHistory)
        )
        == 0
    )


# --- Preview ----------------------------------------------------------------


async def test_the_preview_writes_nothing(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    result = await preview_raw_jobs(
        db_session,
        portal_slug=alpha.slug,
        raw_jobs=[make_raw(source=alpha.slug)],
        now=NOW,
    )

    assert len(result.jobs) == 1
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0
    assert await db_session.scalar(select(func.count()).select_from(JobSource)) == 0


async def test_the_preview_groups_listings_that_would_merge(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, _ = portals

    result = await preview_raw_jobs(
        db_session,
        portal_slug=alpha.slug,
        raw_jobs=[
            make_raw(source=alpha.slug, external_id="p-1"),
            make_raw(source=alpha.slug, external_id="p-2", location="Boston"),
            make_raw(source=alpha.slug, external_id="p-3", company="Fabrikam"),
        ],
        now=NOW,
    )

    assert result.canonical_groups == 2
    assert result.jobs[1].duplicate_of_index == 0
    assert result.jobs[2].duplicate_of_index is None


async def test_the_preview_reports_an_existing_job_it_would_join(
    db_session: AsyncSession, portals: tuple[Portal, Portal]
) -> None:
    alpha, beta = portals

    stored = await process_raw_jobs(
        db_session, portal=alpha, raw_jobs=[make_raw(source=alpha.slug)], now=NOW
    )
    result = await preview_raw_jobs(
        db_session,
        portal_slug=beta.slug,
        raw_jobs=[make_raw(source=beta.slug)],
        now=NOW,
    )

    assert result.jobs[0].existing_job_id == stored.processed[0].job_id
    assert result.jobs[0].verdict is not None
    assert result.jobs[0].verdict["kind"] == "probable"
