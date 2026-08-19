"""Column widths are enforced during normalisation, not discovered at INSERT.

``MAX_TITLE_LENGTH``, ``MAX_COMPANY_LENGTH`` and ``MAX_LOCATION_LENGTH`` were
declared to match the database columns but never applied, so an over-long value
reached Postgres and raised ``StringDataRightTruncationError``. That is not a
one-record failure: the portal's listings are written inside that portal's
savepoint, so the raise rolled the whole leg back and the portal reported zero.

Found live - a Hiring Cafe posting advertised across twenty-two cities carried a
282-character location, and it cost that portal all 190 listings it had
collected.

These tests pin the limits themselves, and then the property that actually
matters: one over-long record costs at most itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job, RawJobRecord
from app.portals.base import RawJob
from app.processing.normalize import (
    MAX_COMPANY_LENGTH,
    MAX_EMPLOYMENT_TYPE_LENGTH,
    MAX_LOCATION_LENGTH,
    MAX_TITLE_LENGTH,
    NormalizedJob,
    clamp_display,
    clamp_key,
    normalize_raw_job,
)
from app.services import search_run_service
from app.services.job_processing import process_raw_jobs
from tests.factories import make_portal, make_profile, make_resume, make_user

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

DESCRIPTION = (
    "Own the monthly close, build the operating model and partner with the "
    "business on forecasting and budgeting. " * 8
)

#: The real value that broke production, verbatim. 282 characters.
TWENTY_TWO_CITIES = (
    "San Francisco or Denver or Atlanta or Chicago or Salt Lake City or "
    "Seattle or Phoenix or Los Angeles or Washington or Miami or "
    "Indianapolis or New Orleans or Boston or Minneapolis or Kansas City or "
    "Albuquerque or New York or Oklahoma City or Portland or Austin or "
    "Woodlawn or Dallas"
)


def listing(
    *,
    title: str = "Financial Analyst",
    company: str | None = "Contoso Ltd",
    location: str = "Austin, TX, USA",
    external_id: str = "ext-1",
    url: str = "https://hiringcafe.com/jobs/financial-analyst",
) -> RawJob:
    return RawJob(
        external_id=external_id,
        source="hiringcafe",
        title=title,
        company=company,
        location=location,
        # Keyed on the id so separate listings stay separate: an identical
        # description fingerprint would deduplicate them into one job and the
        # batch-survival tests below would be measuring the wrong thing.
        description=f"{DESCRIPTION} Reference {external_id}.",
        source_url=url,
        # Per listing, like the description: a shared application URL
        # canonicalises to one vacancy, which is correct behaviour but would
        # collapse these into a single job.
        application_url=f"https://boards.greenhouse.io/acme/jobs/{external_id}",
        posted_at=NOW - timedelta(days=1),
        employment_type="Full-time",
        raw_payload={},
    )


# --- The clamps themselves ---------------------------------------------------


def test_the_real_location_that_broke_production_now_fits() -> None:
    assert len(TWENTY_TWO_CITIES) == 282, "the fixture must stay the real value"

    clamped = clamp_display(TWENTY_TWO_CITIES, MAX_LOCATION_LENGTH)

    assert clamped is not None
    assert len(clamped) <= MAX_LOCATION_LENGTH


def test_a_value_within_its_limit_is_untouched() -> None:
    assert clamp_display("Austin, TX", MAX_LOCATION_LENGTH) == "Austin, TX"


def test_nothing_stays_nothing() -> None:
    assert clamp_display(None, MAX_LOCATION_LENGTH) is None


def test_trimming_stops_on_a_word_boundary() -> None:
    """A mid-word cut reads as corrupted data rather than a shortened list."""
    clamped = clamp_display(TWENTY_TWO_CITIES, MAX_LOCATION_LENGTH)

    assert clamped is not None
    # Every word kept is a whole word from the original.
    assert TWENTY_TWO_CITIES.startswith(clamped)
    assert not clamped.endswith(" ")


def test_a_trimmed_list_does_not_end_on_a_dangling_joiner() -> None:
    clamped = clamp_display(TWENTY_TWO_CITIES, MAX_LOCATION_LENGTH)

    assert clamped is not None
    assert not clamped.casefold().endswith(" or")
    assert not clamped.casefold().endswith(" and")
    assert clamped[-1] not in ",;|/-&"


def test_a_single_word_longer_than_the_column_is_still_cut() -> None:
    """There is no word boundary to fall back to, so it has to be sliced."""
    clamped = clamp_display("x" * 400, MAX_LOCATION_LENGTH)

    assert clamped is not None
    assert len(clamped) == MAX_LOCATION_LENGTH


def test_a_comparison_key_is_sliced_not_word_trimmed() -> None:
    """A key is compared, never read, and must cut identically every time."""
    key = "financial analyst " * 50

    assert len(clamp_key(key, MAX_TITLE_LENGTH)) == MAX_TITLE_LENGTH
    assert clamp_key(key, MAX_TITLE_LENGTH) == clamp_key(key, MAX_TITLE_LENGTH)


# --- Normalisation applies them ----------------------------------------------


def test_an_overlong_location_is_normalised_rather_than_rejected() -> None:
    result = normalize_raw_job(listing(location=TWENTY_TWO_CITIES), now=NOW)

    assert isinstance(result, NormalizedJob), "a long location is not a rejection"
    assert result.location.display is not None
    assert len(result.location.display) <= MAX_LOCATION_LENGTH


def test_an_overlong_title_fits_its_column() -> None:
    result = normalize_raw_job(listing(title="Senior Financial Analyst " * 40), now=NOW)

    assert isinstance(result, NormalizedJob)
    assert len(result.title.display) <= MAX_TITLE_LENGTH
    assert len(result.title.key) <= MAX_TITLE_LENGTH


def test_an_overlong_company_fits_its_column() -> None:
    result = normalize_raw_job(listing(company="Contoso Holdings " * 30), now=NOW)

    assert isinstance(result, NormalizedJob)
    assert result.company is not None
    assert len(result.company.display) <= MAX_COMPANY_LENGTH
    assert len(result.company.key) <= MAX_COMPANY_LENGTH


def test_an_overlong_employment_type_still_fits() -> None:
    """The one limit that was already enforced must stay enforced."""
    result = normalize_raw_job(listing(), now=NOW)

    assert isinstance(result, NormalizedJob)
    assert result.employment_type is not None
    assert len(result.employment_type) <= MAX_EMPLOYMENT_TYPE_LENGTH


def test_clamping_does_not_change_the_us_verdict() -> None:
    """The gate reads the clamped value, so what is judged is what is stored."""
    result = normalize_raw_job(listing(location=TWENTY_TWO_CITIES), now=NOW)

    assert isinstance(result, NormalizedJob), "US cities must still read as US"


def test_a_normalised_job_is_still_rejected_for_a_real_reason() -> None:
    """Clamping must not turn a genuine rejection into an accepted job."""
    result = normalize_raw_job(listing(location="Berlin, Germany"), now=NOW)

    assert not isinstance(result, NormalizedJob)


# --- The property that matters: the leg survives -----------------------------


async def test_one_overlong_location_does_not_roll_back_the_portal_leg(
    db_session: AsyncSession,
) -> None:
    """The regression. This used to raise StringDataRightTruncationError.

    The listing with the 282-character location sits in the middle of the batch,
    exactly as it did live, so a failure would take the listings on both sides
    of it with it.
    """
    portal = await make_portal(db_session, slug="hc-limits", is_implemented=True)

    result = await process_raw_jobs(
        db_session,
        portal=portal,
        raw_jobs=[
            listing(
                external_id="before",
                url="https://hiringcafe.com/jobs/a",
                company="Northwind Traders",
            ),
            listing(
                external_id="wide",
                url="https://hiringcafe.com/jobs/b",
                title="Budget Analyst",
                company="Centers for Medicare Services",
                location=TWENTY_TWO_CITIES,
            ),
            listing(
                external_id="after",
                url="https://hiringcafe.com/jobs/c",
                company="Fabrikam Inc",
            ),
        ],
        now=NOW,
    )
    await db_session.flush()

    assert len(result.processed) == 3, "no listing was lost"
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 3

    stored = (await db_session.scalars(select(Job))).all()
    for job in stored:
        assert job.location is None or len(job.location) <= MAX_LOCATION_LENGTH
        assert len(job.title) <= MAX_TITLE_LENGTH

    # The wide listing is genuinely present, not merely absent-without-error.
    wide = next(job for job in stored if job.title == "Budget Analyst")
    assert wide.location is not None
    assert TWENTY_TWO_CITIES.startswith(wide.location)


async def test_the_audit_row_is_written_within_its_columns_too(
    db_session: AsyncSession,
) -> None:
    """``raw_jobs`` bypasses normalisation, and shares the same column widths.

    It is written in the same transaction, so an over-long value here would
    roll the leg back just as surely as the job row would.
    """
    owner = await make_user(db_session)
    resume = await make_resume(db_session, user=owner)
    await make_profile(db_session, resume=resume)
    portal = await make_portal(db_session, slug="hc-audit", is_implemented=True)
    prepared = await search_run_service.create_search_run(
        db_session,
        resume=resume,
        portal_ids=[portal.id],
        requested_limit=20,
        require_adapter=False,
    )

    await process_raw_jobs(
        db_session,
        portal=portal,
        raw_jobs=[listing(external_id="wide", location=TWENTY_TWO_CITIES)],
        search_run_portal=prepared.legs[0],
        now=NOW,
    )
    await db_session.flush()

    rows = (await db_session.scalars(select(RawJobRecord))).all()
    assert rows
    for row in rows:
        assert row.location is None or len(row.location) <= MAX_LOCATION_LENGTH
        assert len(row.title) <= MAX_TITLE_LENGTH


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Senior Financial Analyst " * 40),
        ("company", "Contoso Holdings " * 30),
        ("location", TWENTY_TWO_CITIES),
    ],
)
async def test_no_overlong_field_can_fail_a_batch(
    db_session: AsyncSession, field: str, value: str
) -> None:
    portal = await make_portal(db_session, slug=f"hc-{field}", is_implemented=True)

    result = await process_raw_jobs(
        db_session,
        portal=portal,
        raw_jobs=[listing(**{field: value})],
        now=NOW,
    )
    await db_session.flush()

    assert len(result.processed) == 1
