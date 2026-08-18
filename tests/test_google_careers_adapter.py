"""Google Careers adapter.

No test here reaches the network. Parsing is driven from a recorded live page
(``tests/fixtures/portals/google_careers_results.html``), and the pipeline
tests drive the real adapter over a stubbed transport, so the suite stays
deterministic and independent of what Google is advertising today.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import ApplicationType, FreshnessWindow
from app.portals.adapters.google_careers import (
    DEFAULT_LOCATION,
    RESULTS_URL,
    GoogleCareersPortal,
    build_description,
    format_location,
    parse_results,
    parse_timestamp,
    strip_html,
    title_matches,
    title_tokens,
)
from app.portals.base import APPLICATION_TYPE_KEY, PortalSearchRequest, RawJob
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.portals.registry import registry
from app.processing.us_locations import classify_us_location
from app.search.freshness import is_eligible_age

FIXTURE = (
    Path(__file__).parent / "fixtures" / "portals" / "google_careers_results.html"
)

#: The moment the fixture was recorded, so age assertions stay stable.
NOW = datetime(2026, 8, 18, 18, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def portal() -> GoogleCareersPortal:
    return GoogleCareersPortal({})


@pytest.fixture
def stub_page(monkeypatch: pytest.MonkeyPatch):
    """Serve a body from the adapter's HTTP client without touching the network."""

    def _apply(body: str | Exception, *, record: list[str] | None = None):
        async def _get_text(self, url, *, params=None):  # noqa: ANN001
            if record is not None:
                record.append(f"{url}?{params}")
            if isinstance(body, Exception):
                raise body
            return body

        monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _get_text)

    return _apply


def make_request(*queries: str, limit: int = 20) -> PortalSearchRequest:
    return PortalSearchRequest(
        queries=queries or ("Financial Analyst",),
        window=FreshnessWindow.LAST_30D,
        limit=limit,
    )


def wrap(records: Any, *, total: int = 99, page_size: int = 20) -> str:
    """Wrap records in the exact data island Google serves."""
    payload = json.dumps([records, None, total, page_size], ensure_ascii=False)
    return (
        '<html><body><script class="ds:1">AF_initDataCallback({key: \'ds:1\', '
        f"hash: '2', data:{payload}, sideChannel: {{}}}});</script></body></html>"
    )


# --- Registration and capabilities ------------------------------------------


def test_the_adapter_is_registered_under_its_slug() -> None:
    assert registry.is_registered("google_careers")
    assert registry.get("google_careers") is GoogleCareersPortal


def test_the_registry_builds_it_with_its_options() -> None:
    built = registry.create("google_careers", {"location": "United States"})

    assert isinstance(built, GoogleCareersPortal)
    assert built.location == "United States"


def test_it_declares_what_it_can_actually_do(portal: GoogleCareersPortal) -> None:
    capabilities = portal.capabilities

    # sort_by=date orders on the refresh timestamp, so it is not a date filter.
    assert capabilities.supports_date_filter is False
    # The full posting text arrives with the listing.
    assert capabilities.supports_description_fetch is False
    # Not absent - disallowed by robots.txt.
    assert capabilities.supports_pagination is False
    assert capabilities.requires_auth is False
    assert capabilities.provides_application_url is True


def test_it_needs_no_credentials(portal: GoogleCareersPortal) -> None:
    assert portal.capabilities.requires_auth is False


# --- Request construction ----------------------------------------------------


async def test_it_requests_the_robots_allowed_results_page(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    seen: list[str] = []
    stub_page(page, record=seen)

    await portal.search(make_request("Financial Analyst"))

    assert len(seen) == 1
    assert seen[0].startswith(RESULTS_URL)
    assert "'q': 'Financial Analyst'" in seen[0]


async def test_it_never_requests_a_paginated_url(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    """robots.txt disallows ?page= four ways. It must never be sent."""
    seen: list[str] = []
    stub_page(page, record=seen)

    await portal.search(make_request("Financial Analyst", "Data Analyst"))

    for call in seen:
        assert "page=" not in call, call
        assert "'page'" not in call, call


async def test_the_us_location_filter_is_sent_by_default(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    seen: list[str] = []
    stub_page(page, record=seen)

    await portal.search(make_request("Financial Analyst"))

    assert f"'location': '{DEFAULT_LOCATION}'" in seen[0]


async def test_the_location_is_configurable(page: str, stub_page) -> None:
    seen: list[str] = []
    stub_page(page, record=seen)

    await GoogleCareersPortal({"location": "Canada"}).search(make_request("Analyst"))

    assert "'location': 'Canada'" in seen[0]


async def test_one_request_is_made_per_title_variation(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    seen: list[str] = []
    stub_page(page, record=seen)

    await portal.search(make_request("Financial Analyst", "FP&A Analyst", limit=100))

    assert len(seen) == 2


# --- Parsing the recorded page ----------------------------------------------


def test_the_recorded_page_yields_records(page: str) -> None:
    records = parse_results(page)

    assert len(records) == 7


async def test_it_maps_the_recorded_page_onto_raw_jobs(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(page)

    result = await portal.search(make_request("Financial Analyst"))

    assert result.jobs
    job = result.jobs[0]
    assert isinstance(job, RawJob)
    assert job.source == "google_careers"
    assert job.title
    assert job.company
    assert job.location
    assert job.description
    assert job.external_id


async def test_every_returned_job_carries_a_full_description(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    """Card snippets are not enough; the scorer needs the posting text."""
    stub_page(page)

    result = await portal.search(make_request("Analyst", limit=100))

    for job in result.jobs:
        assert job.description
        # Every recorded posting runs to thousands of characters.
        assert len(job.description) > 1000, job.title


def test_the_description_carries_all_three_sections(page: str) -> None:
    record = parse_results(page)[0]

    description = build_description(record)

    assert description is not None
    assert "Responsibilities" in description
    assert "Qualifications" in description


def test_html_markup_is_reduced_to_readable_text() -> None:
    text = strip_html("<h3>Minimum:</h3><ul>\n<li>BSc<br></li>\n<li>4 years</li></ul>")

    assert "<" not in text
    assert "Minimum:" in text
    assert "- BSc" in text
    assert "- 4 years" in text


def test_html_entities_are_unescaped() -> None:
    assert strip_html("<p>R&amp;D &gt; everything</p>") == "R&D > everything"


def test_an_empty_description_becomes_none() -> None:
    assert build_description(["1", "Title"]) is None


# --- Dates: the field that had to be proved ---------------------------------


def test_posted_at_uses_the_publication_timestamp_not_the_refresh_one(
    page: str,
) -> None:
    """The core date guarantee.

    Three recorded listings were refreshed long after publication, and Google's
    own sort_by=date ranks them by the refresh. Reading that field would date a
    January posting to August - exactly the crawl-date substitution CLAUDE.md
    section 6 forbids.
    """
    by_id = {r[0]: r for r in parse_results(page)}
    portal = GoogleCareersPortal({})

    record = by_id["128047698732294854"]
    published, refreshed = record[12], record[13]
    assert published != refreshed, "this fixture row must have diverging timestamps"

    job = portal._to_raw_job(record)

    assert job is not None
    assert job.posted_at == datetime.fromtimestamp(published[0], UTC)
    assert job.posted_at != datetime.fromtimestamp(refreshed[0], UTC)
    # And concretely: it is the old date, not today's.
    assert job.posted_at.year == 2026
    assert job.posted_at.month == 1


def test_a_stale_posting_refreshed_today_is_still_excluded_by_the_window(
    page: str,
) -> None:
    """The visible consequence, asserted rather than assumed."""
    by_id = {r[0]: r for r in parse_results(page)}
    portal = GoogleCareersPortal({})

    job = portal._to_raw_job(by_id["128047698732294854"])

    assert job is not None
    assert is_eligible_age(job.posted_at, NOW) is False


def test_a_recently_published_posting_is_eligible(page: str) -> None:
    by_id = {r[0]: r for r in parse_results(page)}
    portal = GoogleCareersPortal({})

    job = portal._to_raw_job(by_id["121773752340882118"])

    assert job is not None
    assert is_eligible_age(job.posted_at, NOW) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "2026-08-18",
        [None],
        ["not-a-number"],
        [0],
        [-1],
        [True],
        {},
        [10**18],
    ],
)
def test_an_unusable_timestamp_yields_no_date(value: Any) -> None:
    assert parse_timestamp(value) is None


def test_a_valid_timestamp_is_read_as_utc() -> None:
    parsed = parse_timestamp([1787074971, 706000000])

    assert parsed == datetime(2026, 8, 18, 17, 42, 51, tzinfo=UTC)
    assert parsed.tzinfo is UTC


def test_a_record_without_a_date_keeps_posted_at_empty() -> None:
    portal = GoogleCareersPortal({})
    record: list[Any] = ["77", "Analyst"] + [None] * 20

    job = portal._to_raw_job(record)

    assert job is not None
    assert job.posted_at is None


# --- Locations ---------------------------------------------------------------


def test_the_us_location_is_preferred_when_several_are_listed() -> None:
    record: list[Any] = [None] * 10
    record[9] = [
        ["Zurich, Switzerland", ["Zurich"], "Zurich", None, None, "CH"],
        ["Austin, TX, USA", ["Austin"], "Austin", None, "TX", "US"],
    ]

    assert format_location(record) == "Austin, TX, USA"


def test_the_first_location_is_used_when_none_are_us() -> None:
    record: list[Any] = [None] * 10
    record[9] = [["Zurich, Switzerland", ["Zurich"], "Zurich", None, None, "CH"]]

    assert format_location(record) == "Zurich, Switzerland"


def test_a_location_is_composed_when_no_formatted_address_was_published() -> None:
    record: list[Any] = [None] * 10
    record[9] = [[None, None, "Austin", None, "TX", "US"]]

    assert format_location(record) == "Austin, TX, US"


@pytest.mark.parametrize("locations", [None, [], "Austin", [[]], [None], 42])
def test_an_unusable_locations_field_yields_no_location(locations: Any) -> None:
    record: list[Any] = [None] * 10
    record[9] = locations

    assert format_location(record) is None


def test_recorded_locations_are_accepted_by_the_existing_us_classifier(
    page: str,
) -> None:
    """The adapter emits strings the shared classifier already understands."""
    portal = GoogleCareersPortal({})

    for record in parse_results(page):
        job = portal._to_raw_job(record)
        assert job is not None
        assert classify_us_location(job.location).is_us is True, job.location


# --- Identity and URLs -------------------------------------------------------


def test_the_external_id_is_googles_own_stable_id(page: str) -> None:
    portal = GoogleCareersPortal({})
    record = parse_results(page)[0]

    job = portal._to_raw_job(record)

    assert job is not None
    assert job.external_id == record[0]
    assert job.external_id.isdigit()


def test_the_url_is_built_from_the_id_with_no_invented_slug(page: str) -> None:
    portal = GoogleCareersPortal({})
    record = parse_results(page)[0]

    job = portal._to_raw_job(record)

    assert job is not None
    assert job.source_url == f"{RESULTS_URL}/{record[0]}"
    assert job.application_url == job.source_url
    # Nothing derived from the title leaked into the URL.
    assert job.title.split()[0].lower() not in job.source_url.lower()


def test_the_job_url_is_recognised_as_the_employers_own_page(page: str) -> None:
    from app.processing.urls import JobUrlKind, classify_url

    portal = GoogleCareersPortal({})
    job = portal._to_raw_job(parse_results(page)[0])

    assert job is not None
    kind = classify_url(job.source_url, company=job.company)
    # google.com is demonstrably Google's own host, so the shared classifier
    # ranks it as employer-direct without this adapter asserting anything.
    assert kind is JobUrlKind.EMPLOYER_CAREER


def test_the_application_type_is_declared(page: str) -> None:
    portal = GoogleCareersPortal({})

    job = portal._to_raw_job(parse_results(page)[0])

    assert job is not None
    assert (
        job.raw_payload[APPLICATION_TYPE_KEY] == ApplicationType.LONG_APPLICATION.value
    )


def test_no_employment_type_is_invented(page: str) -> None:
    portal = GoogleCareersPortal({})

    job = portal._to_raw_job(parse_results(page)[0])

    assert job is not None
    assert job.employment_type is None


@pytest.mark.parametrize(
    "record",
    [
        [],
        [None, "Analyst"],
        ["", "Analyst"],
        ["123"],
        ["123", ""],
        ["123", None],
        [{"id": 1}, "Analyst"],
    ],
)
def test_a_record_without_an_id_and_title_is_dropped(record: list[Any]) -> None:
    assert GoogleCareersPortal({})._to_raw_job(record) is None


def test_an_integer_id_is_accepted() -> None:
    job = GoogleCareersPortal({})._to_raw_job([121773752340882118, "Analyst"])

    assert job is not None
    assert job.external_id == "121773752340882118"


# --- Title filtering ---------------------------------------------------------


def test_title_tokens_ignore_single_letters() -> None:
    assert title_tokens("FP&A Analyst") == frozenset({"fp", "analyst"})


@pytest.mark.parametrize(
    ("query", "title", "expected"),
    [
        ("Financial Analyst", "Senior Financial Analyst", True),
        ("Financial Analyst", "Financial Analyst II, Marketing Finance", True),
        ("Financial Analyst", "Business Analyst", False),
        ("Data Analyst", "Novel AI Lead Methodologist", False),
        ("Data Analyst", "Senior Data Analyst, Search", True),
        ("", "Anything", False),
        ("Analyst", "", False),
    ],
)
def test_only_titles_answering_the_query_are_kept(
    query: str, title: str, expected: bool
) -> None:
    assert title_matches(query, title) is expected


async def test_googles_semantic_results_are_narrowed_by_the_title_filter(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    """Google answers "Financial Analyst" with strategy and engineering roles."""
    stub_page(page)

    result = await portal.search(make_request("Financial Analyst", limit=100))

    titles = [job.title for job in result.jobs]
    assert titles, "the fixture must contain at least one financial analyst role"
    for title in titles:
        assert "financial" in title.casefold()
        assert "analyst" in title.casefold()


# --- Empty, malformed and failing responses ---------------------------------


async def test_a_page_with_no_results_block_is_an_empty_result_not_an_error(
    stub_page, portal: GoogleCareersPortal
) -> None:
    """Google serves this for a query nothing matches."""
    stub_page("<html><body>No jobs matched your search.</body></html>")

    result = await portal.search(make_request("Underwater Basket Weaver"))

    assert result.jobs == []
    assert result.has_more is False


async def test_an_empty_records_array_returns_no_jobs(
    stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(wrap([], total=0))

    assert (await portal.search(make_request())).jobs == []


async def test_a_null_records_array_returns_no_jobs(
    stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(wrap(None, total=0))

    assert (await portal.search(make_request())).jobs == []


def test_an_undecodable_results_block_is_a_parse_error() -> None:
    body = (
        '<script class="ds:1">AF_initDataCallback({key: \'ds:1\', hash: \'2\', '
        "data:[[[not json, sideChannel: {}});</script>"
    )

    with pytest.raises(PortalParseError):
        parse_results(body)


@pytest.mark.parametrize("payload", ["[]", '"a string"', "{}", "null", "5"])
def test_a_results_block_of_the_wrong_shape_is_a_parse_error(payload: str) -> None:
    body = (
        '<script class="ds:1">AF_initDataCallback({key: \'ds:1\', hash: \'2\', '
        f"data:{payload}, sideChannel: {{}}}});</script>"
    )

    with pytest.raises(PortalParseError):
        parse_results(body)


def test_a_records_field_that_is_not_a_list_is_a_parse_error() -> None:
    with pytest.raises(PortalParseError):
        parse_results(wrap("not-a-list"))


def test_non_list_records_are_skipped_rather_than_crashing() -> None:
    records = parse_results(wrap([["1", "Analyst"], "junk", None, 42]))

    assert len(records) == 1


async def test_a_parse_error_is_raised_not_swallowed(
    stub_page, portal: GoogleCareersPortal
) -> None:
    """A layout change must surface, not quietly return zero jobs."""
    stub_page(wrap("not-a-list"))

    with pytest.raises(PortalParseError):
        await portal.search(make_request())


@pytest.mark.parametrize(
    "error",
    [
        PortalTimeoutError("timed out", portal="google_careers"),
        PortalBlockedError("blocked", portal="google_careers"),
        PortalRateLimitedError("slow down", portal="google_careers"),
        PortalError("boom", portal="google_careers"),
    ],
)
async def test_every_transport_failure_surfaces_as_a_portal_error(
    stub_page, portal: GoogleCareersPortal, error: PortalError
) -> None:
    stub_page(error)

    with pytest.raises(PortalError):
        await portal.search(make_request("Financial Analyst"))


async def test_one_failing_query_does_not_lose_the_others(
    page: str, portal: GoogleCareersPortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _get_text(self, url, *, params=None):  # noqa: ANN001
        calls.append(params["q"])
        if params["q"] == "Broken Query":
            raise PortalTimeoutError("timed out", portal="google_careers")
        return page

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _get_text)

    result = await portal.search(
        make_request("Broken Query", "Financial Analyst", limit=100)
    )

    assert calls == ["Broken Query", "Financial Analyst"]
    assert result.jobs, "the surviving query must still contribute"


async def test_a_health_check_reports_rather_than_raising(
    stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(PortalTimeoutError("timed out", portal="google_careers"))

    health = await portal.health_check()

    assert health.is_available is False
    assert health.detail


async def test_a_healthy_portal_reports_available(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(page)

    assert (await portal.health_check()).is_available is True


# --- Pagination, limits and duplicates --------------------------------------


async def test_it_never_claims_a_further_page(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(page)

    result = await portal.search(make_request("Analyst", limit=100))

    assert result.has_more is False
    assert result.next_cursor is None


async def test_the_requested_limit_is_honoured(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(page)

    result = await portal.search(make_request("Analyst", limit=2))

    assert len(result.jobs) == 2


async def test_the_same_job_returned_by_two_queries_appears_once(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(page)

    result = await portal.search(
        make_request("Financial Analyst", "Financial Analyst", limit=100)
    )

    ids = [job.external_id for job in result.jobs]
    assert len(ids) == len(set(ids))


async def test_a_duplicate_inside_one_response_appears_once(
    stub_page, portal: GoogleCareersPortal
) -> None:
    record = ["55", "Financial Analyst"] + [None] * 20
    stub_page(wrap([record, list(record)]))

    result = await portal.search(make_request("Financial Analyst"))

    assert len(result.jobs) == 1


async def test_the_window_is_reported_as_applied_by_the_caller(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    """Google's own date sort ranks on refresh, so it cannot filter for us."""
    stub_page(page)

    result = await portal.search(make_request("Analyst"))

    assert result.window_applied_by_portal is False
    assert result.window is FreshnessWindow.LAST_30D


async def test_fetch_description_returns_what_already_arrived(
    page: str, stub_page, portal: GoogleCareersPortal
) -> None:
    stub_page(page)
    job = (await portal.search(make_request("Analyst"))).jobs[0]

    assert await portal.fetch_description(job) == job.description


# --- Through the existing pipeline ------------------------------------------
#
# The adapter is only half the requirement: Google listings have to reach the
# same gates every other portal's listings pass. These tests run real search
# runs with the real GoogleCareersPortal over a stubbed transport.

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.constants import Seniority, SkillKind  # noqa: E402
from app.models.profile import ResumeSkill  # noqa: E402
from app.services import search_run_service  # noqa: E402
from app.services.search_orchestrator import execute_search_run  # noqa: E402
from tests.factories import (  # noqa: E402
    make_portal,
    make_profile,
    make_resume,
    make_user,
)
from tests.fake_portals import build_fake_portal, make_raw_job  # noqa: E402

RESUME_SKILLS = ("excel", "sql", "forecasting", "budgeting", "financial modeling")

FINANCE_TEXT = """About the job
We are hiring a Financial Analyst to own forecasting and the monthly close.

Responsibilities
- Draw interpretable insights from sophisticated business analyses.
- Own the forecasting cycle and the monthly close.

Qualifications
- Bachelor's degree or equivalent practical experience.
- 3 years of experience in financial analysis.
- Advanced Excel and SQL.
- Forecasting and budgeting.

Additional context about the team and the work follows here to keep this
description comfortably above the minimum length for scoring.
"""


async def analysed_resume(session: AsyncSession, *, user=None):
    """A Financial Analyst resume with enough profile to be scoreable."""
    owner = user or await make_user(session)
    resume = await make_resume(session, user=owner)
    profile = await make_profile(session, resume=resume)
    profile.years_experience = 5.0
    profile.seniority = Seniority.MID
    profile.education = ["Bachelor of Science in Finance"]
    for key in RESUME_SKILLS:
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
    return owner, resume


def google_payload(
    *,
    published: datetime,
    count: int = 3,
    start_id: int = 900,
    location: list[Any] | None = None,
) -> str:
    """A page shaped exactly like the recorded one."""
    stamp = [int(published.timestamp()), 0]
    refreshed = [int(NOW.timestamp()), 0]
    records = []
    for index in range(count):
        record: list[Any] = [None] * 21
        record[0] = str(start_id + index)
        record[1] = "Financial Analyst"
        record[3] = [None, "<ul><li>Own the forecasting cycle.</li></ul>"]
        record[4] = [None, "<ul><li>3 years of financial analysis.</li></ul>"]
        record[7] = "Google"
        record[9] = [
            location
            or ["Austin, TX, USA", ["Austin, TX, USA"], "Austin", None, "TX", "US"]
        ]
        record[10] = [None, f"<p>{FINANCE_TEXT}</p>"]
        record[12] = stamp
        # Deliberately refreshed "now", so a pipeline reading the wrong field
        # would wrongly admit an over-age posting.
        record[13] = refreshed
        record[14] = refreshed
        records.append(record)
    return wrap(records, total=count)


@pytest.fixture
def google_live(monkeypatch: pytest.MonkeyPatch):
    """Drive the real adapter from a page body instead of the network."""

    def _apply(body: str):
        async def _get_text(self, url, *, params=None):  # noqa: ANN001
            return body

        monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _get_text)

    return _apply


async def build_run(session, *, resume, portal_specs, limit=20):
    """Create a run over the given (slug, adapter_instance) pairs."""
    portal_ids = []
    adapters = {}
    for slug, adapter in portal_specs:
        portal = await make_portal(session, slug=slug, adapter_key=slug, options={})
        portal_ids.append(portal.id)
        adapters[portal.id] = adapter
    prepared = await search_run_service.create_search_run(
        session,
        resume=resume,
        portal_ids=portal_ids,
        requested_limit=limit,
        require_adapter=False,
    )
    return prepared, adapters


async def test_google_jobs_pass_the_normal_scoring_pipeline(
    db_session: AsyncSession, google_live
) -> None:
    google_live(google_payload(published=NOW - timedelta(days=3)))
    _, resume = await analysed_resume(db_session)

    prepared, adapters = await build_run(
        db_session,
        resume=resume,
        portal_specs=[("google_careers", GoogleCareersPortal({}))],
    )
    outcome = await execute_search_run(
        db_session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=NOW,
    )

    assert outcome.returned_count > 0
    for item in outcome.results:
        assert item.final_score >= 70.0
        assert is_eligible_age(item.posted_at, NOW)
        assert item.preferred_application_url
        assert "google.com/about/careers" in item.preferred_application_url


async def test_a_posting_published_long_ago_is_excluded_even_when_refreshed_today(
    db_session: AsyncSession, google_live
) -> None:
    """The date decision, proved end to end.

    Every record here was refreshed "now" and published 45 days ago. A pipeline
    reading Google's own sort key would admit all of them.
    """
    google_live(google_payload(published=NOW - timedelta(days=45)))
    _, resume = await analysed_resume(db_session)

    prepared, adapters = await build_run(
        db_session,
        resume=resume,
        portal_specs=[("google_careers", GoogleCareersPortal({}))],
    )
    outcome = await execute_search_run(
        db_session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=NOW,
    )

    assert outcome.returned_count == 0


async def test_a_non_us_google_job_is_excluded_by_the_existing_us_rule(
    db_session: AsyncSession, google_live
) -> None:
    google_live(
        google_payload(
            published=NOW - timedelta(days=2),
            location=["Zurich, Switzerland", ["Zurich"], "Zurich", None, None, "CH"],
        )
    )
    _, resume = await analysed_resume(db_session)

    prepared, adapters = await build_run(
        db_session,
        resume=resume,
        portal_specs=[("google_careers", GoogleCareersPortal({}))],
    )
    outcome = await execute_search_run(
        db_session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=NOW,
    )

    assert outcome.returned_count == 0
    assert outcome.portals[0].non_us_count > 0


async def test_a_mixed_search_returns_both_portals(
    db_session: AsyncSession, google_live
) -> None:
    google_live(google_payload(published=NOW - timedelta(days=4)))
    _, resume = await analysed_resume(db_session)

    other = build_fake_portal(
        "otherboard",
        jobs=[
            make_raw_job(
                portal="otherboard",
                index=index,
                company=f"Other Co {index}",
                location="Chicago, IL",
                now=NOW,
            )
            for index in range(3)
        ],
    )

    prepared, adapters = await build_run(
        db_session,
        resume=resume,
        portal_specs=[
            ("otherboard", other()),
            ("google_careers", GoogleCareersPortal({})),
        ],
    )
    outcome = await execute_search_run(
        db_session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=NOW,
    )

    contributing = {leg.portal_slug for leg in outcome.portals if leg.qualified_count}
    assert len(contributing) == 2, contributing
    assert outcome.returned_count > 0


async def test_adding_google_does_not_alter_the_other_portal_results(
    db_session: AsyncSession, google_live
) -> None:
    """The regression guard: the same search, with and without Google.

    The existing portal's results must be identical - same jobs, same scores,
    same order. Google adds to the pool; it does not disturb it.
    """
    google_live(google_payload(published=NOW - timedelta(days=4)))
    listings = [
        make_raw_job(
            portal="steady",
            index=index,
            company=f"Steady Co {index}",
            location="Chicago, IL",
            age_days=float(index),
            now=NOW,
        )
        for index in range(4)
    ]

    _, first_resume = await analysed_resume(db_session)
    prepared, adapters = await build_run(
        db_session,
        resume=first_resume,
        portal_specs=[("steady1", build_fake_portal("steady1", jobs=listings)())],
    )
    alone = await execute_search_run(
        db_session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=NOW,
    )

    _, second_resume = await analysed_resume(db_session)
    prepared2, adapters2 = await build_run(
        db_session,
        resume=second_resume,
        portal_specs=[
            ("steady2", build_fake_portal("steady2", jobs=listings)()),
            ("google_careers", GoogleCareersPortal({})),
        ],
    )
    together = await execute_search_run(
        db_session,
        run=prepared2.run,
        legs=prepared2.legs,
        portals=prepared2.portals,
        adapters=adapters2,
        now=NOW,
    )

    def steady_only(outcome):
        return [
            (item.title, item.company, item.final_score)
            for item in outcome.results
            if item.company and item.company.startswith("Steady Co")
        ]

    assert steady_only(alone), "the control run must actually return jobs"
    assert steady_only(together) == steady_only(alone)
    assert together.returned_count > alone.returned_count
