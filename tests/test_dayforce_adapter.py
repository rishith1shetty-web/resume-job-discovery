"""Dayforce adapter: CSRF session, board configuration, parsing, failures.

Every response here comes from the captured fixture or a stub. Nothing in this
file makes a network request, so the suite does not depend on Dayforce being
online.

Two properties get the most attention, because they are the ones that would
quietly corrupt results rather than fail loudly:

* a posting without a usable publication timestamp keeps ``posted_at`` empty,
  so the existing freshness gate excludes it instead of it being given a date;
* the employer name comes from the board's configuration, because the payload
  carries none - and a board configured without one yields no company rather
  than a guessed one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import ApplicationType, FreshnessWindow
from app.portals.adapters.dayforce import (
    CSRF_URL,
    DEFAULT_BOARDS,
    SITE_BASE,
    DayforceBoard,
    DayforcePortal,
    format_location,
    parse_timestamp,
    title_matches,
    title_tokens,
)
from app.portals.base import APPLICATION_TYPE_KEY, PortalSearchRequest
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.portals.registry import registry
from app.processing.urls import classify_url
from app.core.constants import JobUrlKind

FIXTURE = (
    Path(__file__).parent / "fixtures" / "portals" / "dayforce_financial_analyst.json"
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def captured() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def postings(captured: dict[str, Any]) -> list[dict[str, Any]]:
    return captured["golden1/candidateportal"]["jobPostings"]


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]):
    """Serve the CSRF token and each board search from the fixture."""

    def _apply(*, search=None, csrf_payload=None):
        calls: list[dict[str, Any]] = []

        async def _get_json(self, url, *, params=None):  # noqa: ANN001
            calls.append({"method": "GET", "url": url})
            if csrf_payload is not None:
                return csrf_payload
            return {"csrfToken": "test-token"}

        async def _post_json(self, url, *, payload, headers=None):  # noqa: ANN001
            calls.append(
                {"method": "POST", "url": url, "payload": payload, "headers": headers or {}}
            )
            if search is not None:
                return search(url, payload)
            key = f"{payload['clientNamespace']}/{payload['jobBoardCode']}"
            board = captured.get(key)
            if board is None:
                raise PortalError(f"board {key} not found", portal="dayforce")
            return board

        monkeypatch.setattr(
            "app.portals.http.PortalHttpClient.get_json", _get_json, raising=True
        )
        monkeypatch.setattr(
            "app.portals.http.PortalHttpClient.post_json", _post_json, raising=True
        )
        return calls

    return _apply


def one_board(company: str | None = "Golden 1 Credit Union") -> list[dict[str, str | None]]:
    return [{"namespace": "golden1", "board": "candidateportal", "company": company}]


def request(*queries: str, limit: int = 25) -> PortalSearchRequest:
    return PortalSearchRequest(
        queries=queries or ("Financial Analyst",),
        window=FreshnessWindow.LAST_30D,
        limit=limit,
    )


# --- Registration and configuration ----------------------------------------


def test_the_adapter_is_registered_under_its_slug() -> None:
    assert registry.is_registered("dayforce")
    assert registry.get("dayforce") is DayforcePortal
    assert DayforcePortal.slug == "dayforce"
    assert DayforcePortal.display_name == "Dayforce"


def test_capabilities_describe_what_the_api_actually_does() -> None:
    capabilities = DayforcePortal({}).capabilities

    assert capabilities.requires_auth is False
    assert capabilities.provides_application_url is True
    # The full posting text arrives inline, so there is nothing to fetch.
    assert capabilities.supports_description_fetch is False
    # Verified against the live API: every paging parameter is ignored.
    assert capabilities.supports_pagination is False
    assert capabilities.supports_date_filter is False


def test_boards_default_when_nothing_is_configured() -> None:
    portal = DayforcePortal({})

    assert portal.boards
    assert tuple(board.as_tuple() for board in portal.boards) == DEFAULT_BOARDS


def test_boards_come_from_configuration_when_supplied() -> None:
    portal = DayforcePortal({"boards": one_board()})

    assert len(portal.boards) == 1
    assert portal.boards[0].namespace == "golden1"
    assert portal.boards[0].company == "Golden 1 Credit Union"


@pytest.mark.parametrize(
    "configured",
    [
        [],
        "not-a-list",
        [{"namespace": "", "board": "x"}],
        [{"board": "missing-namespace"}],
        [{"namespace": "no-board"}],
        [12345],
    ],
)
def test_malformed_board_configuration_falls_back_to_the_defaults(
    configured: object,
) -> None:
    """One bad row in a config blob must not take the portal down."""
    portal = DayforcePortal({"boards": configured})

    assert tuple(board.as_tuple() for board in portal.boards) == DEFAULT_BOARDS


def test_a_board_may_be_configured_as_a_tuple() -> None:
    portal = DayforcePortal({"boards": [["acme", "careers", "Acme Corp"]]})

    assert portal.boards[0].as_tuple() == ("acme", "careers", "Acme Corp")


# --- The CSRF session ------------------------------------------------------


async def test_the_csrf_token_is_fetched_once_and_sent_on_every_search(stub) -> None:
    calls = stub()
    portal = DayforcePortal({"boards": one_board()})

    await portal.search(request("Financial Analyst", "Senior Financial Analyst"))

    gets = [call for call in calls if call["method"] == "GET"]
    posts = [call for call in calls if call["method"] == "POST"]

    assert [call["url"] for call in gets] == [CSRF_URL]
    assert len(posts) == 2
    for call in posts:
        assert call["headers"]["X-CSRF-TOKEN"] == "test-token"


async def test_a_missing_csrf_token_is_a_parse_error_not_a_crash(stub) -> None:
    stub(csrf_payload={"not-a-token": True})
    portal = DayforcePortal({"boards": one_board()})

    with pytest.raises(PortalParseError):
        await portal.search(request())


def test_the_adapter_keeps_a_cookie_session() -> None:
    """The token alone is refused; it must travel with its cookie."""
    portal = DayforcePortal({})

    assert portal._http.cookies is not None


# --- Collection and parsing ------------------------------------------------


async def test_a_search_returns_normalised_listings(stub) -> None:
    stub()
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())

    assert result.jobs
    job = result.jobs[0]
    assert job.source == "dayforce"
    assert job.title
    assert job.company == "Golden 1 Credit Union"
    assert job.description and len(job.description) > 200
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert job.source_url.startswith(SITE_BASE)
    # No paging exists, so the collector must not be told to ask for more.
    assert result.has_more is False
    assert result.next_cursor is None
    assert result.window_applied_by_portal is False


async def test_the_job_url_is_the_posting_page_and_is_stable(stub) -> None:
    stub()
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())
    job = result.jobs[0]
    posting_id = job.external_id.split(":")[1]

    expected = f"{SITE_BASE}/en-US/golden1/candidateportal/jobs/{posting_id}"
    assert job.source_url == expected
    assert job.application_url == expected
    # Never the search page, and never manufactured from a title.
    assert "/jobs/" in job.source_url
    assert "search" not in job.source_url


async def test_job_ids_are_namespaced_so_two_employers_cannot_collide(stub) -> None:
    stub()
    portal = DayforcePortal(
        {
            "boards": [
                {"namespace": "golden1", "board": "candidateportal", "company": "A"},
                {"namespace": "sarnova", "board": "candidateportal", "company": "B"},
            ]
        }
    )

    result = await portal.search(request())
    ids = [job.external_id for job in result.jobs]

    assert len(ids) == len(set(ids))
    assert all(":" in job_id for job_id in ids)
    assert {job_id.split(":")[0] for job_id in ids} <= {"golden1", "sarnova"}


async def test_the_same_posting_is_not_returned_twice(stub) -> None:
    """Two same-role queries hit the same board; one listing, not two."""
    stub()
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(
        request("Financial Analyst", "Financial Analyst", limit=50)
    )

    ids = [job.external_id for job in result.jobs]
    assert len(ids) == len(set(ids))


async def test_the_limit_is_respected(stub) -> None:
    stub()
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request(limit=2))

    assert len(result.jobs) <= 2


async def test_the_application_type_is_declared_as_a_real_application(stub) -> None:
    stub()
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())

    assert (
        result.jobs[0].raw_payload[APPLICATION_TYPE_KEY]
        == ApplicationType.LONG_APPLICATION.value
    )


async def test_the_search_payload_names_the_board_and_the_culture(stub) -> None:
    calls = stub()
    portal = DayforcePortal({"boards": one_board()})

    await portal.search(request("Financial Analyst"))

    payload = next(call for call in calls if call["method"] == "POST")["payload"]
    assert payload["clientNamespace"] == "golden1"
    assert payload["jobBoardCode"] == "candidateportal"
    assert payload["cultureCode"] == "en-US"
    assert payload["searchText"] == "Financial Analyst"


# --- Posted dates ----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-06T10:00:00+00:00", datetime(2026, 8, 6, 10, 0, tzinfo=UTC)),
        ("2026-08-06T10:00:00Z", datetime(2026, 8, 6, 10, 0, tzinfo=UTC)),
        # Naive input is read as UTC rather than rejected.
        ("2026-08-06T10:00:00", datetime(2026, 8, 6, 10, 0, tzinfo=UTC)),
    ],
)
def test_posting_timestamps_are_parsed_as_timezone_aware_utc(
    value: str, expected: datetime
) -> None:
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-date", 12345, {}])
def test_an_unusable_timestamp_yields_no_date_rather_than_a_guess(value: object) -> None:
    assert parse_timestamp(value) is None


async def test_a_posting_with_no_timestamp_keeps_posted_at_empty(stub) -> None:
    """The freshness gate then excludes it, which is the required behaviour.

    Inventing a date here would slip an undateable listing past the 30-day
    rule, which is exactly what CLAUDE.md section 6 forbids.
    """

    def search(url, payload):  # noqa: ANN001
        return {
            "jobPostings": [
                {
                    "jobPostingId": 1,
                    "jobTitle": "Financial Analyst",
                    "jobDescription": "x" * 400,
                    "postingStartTimestampUTC": None,
                    "postingLocations": [
                        {"formattedAddress": "Austin, TX, USA", "isoCountryCode": "US"}
                    ],
                }
            ],
            "maxCount": 1,
        }

    stub(search=search)
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())

    assert len(result.jobs) == 1
    assert result.jobs[0].posted_at is None

    from app.search.freshness import is_eligible_age

    assert is_eligible_age(result.jobs[0].posted_at, NOW) is False


# --- Locations -------------------------------------------------------------


def test_a_us_location_is_preferred_when_a_posting_lists_several() -> None:
    posting = {
        "postingLocations": [
            {"formattedAddress": "Toronto, ON, Canada", "isoCountryCode": "CA"},
            {"formattedAddress": "Austin, TX, USA", "isoCountryCode": "US"},
        ]
    }

    assert format_location(posting) == "Austin, TX, USA"


def test_a_location_is_built_from_parts_when_not_pre_formatted() -> None:
    posting = {
        "postingLocations": [
            {
                "formattedAddress": "",
                "cityName": "Denver",
                "stateCode": "CO",
                "isoCountryCode": "US",
            }
        ]
    }

    assert format_location(posting) == "Denver, CO, US"


@pytest.mark.parametrize(
    "posting", [{}, {"postingLocations": None}, {"postingLocations": []}, {"postingLocations": "x"}]
)
def test_a_posting_with_no_location_yields_none(posting: dict) -> None:
    assert format_location(posting) is None


async def test_a_non_us_posting_is_collected_and_left_to_the_us_gate(stub) -> None:
    """The adapter does not decide eligibility; normalisation does.

    Collecting it and letting the existing US rule reject it keeps one place in
    the system deciding what "United States" means.
    """

    def search(url, payload):  # noqa: ANN001
        return {
            "jobPostings": [
                {
                    "jobPostingId": 9,
                    "jobTitle": "Financial Analyst",
                    "jobDescription": "y" * 400,
                    "postingStartTimestampUTC": "2026-08-14T00:00:00+00:00",
                    "postingLocations": [
                        {"formattedAddress": "Berlin, Germany", "isoCountryCode": "DE"}
                    ],
                }
            ],
            "maxCount": 1,
        }

    stub(search=search)
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())
    assert result.jobs[0].location == "Berlin, Germany"

    from app.processing.us_locations import classify_us_location

    assert classify_us_location(result.jobs[0].location).is_us is False


# --- Title matching --------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "title", "expected"),
    [
        ("Financial Analyst", "Financial Analyst", True),
        ("Financial Analyst", "Senior Financial Analyst", True),
        ("Financial Analyst", "Financial Analyst II", True),
        # The API's own search is fuzzy and returns these; the adapter does not.
        ("Financial Analyst", "Business Analyst", False),
        ("Network Engineer", "DevOps Engineer", False),
        ("Network Engineer", "Senior Cloud Engineer", False),
        ("", "Financial Analyst", False),
    ],
)
def test_only_titles_that_answer_the_query_are_kept(
    query: str, title: str, expected: bool
) -> None:
    assert title_matches(query, title) is expected


def test_single_letters_are_not_significant_tokens() -> None:
    assert "a" not in title_tokens("FP&A Analyst")


# --- Malformed and failing responses ---------------------------------------


async def test_a_posting_missing_its_id_or_title_is_skipped(stub) -> None:
    def search(url, payload):  # noqa: ANN001
        return {
            "jobPostings": [
                {"jobTitle": "Financial Analyst"},  # no id
                {"jobPostingId": 2},  # no title
                {"jobPostingId": 3, "jobTitle": ""},  # blank title
                {
                    "jobPostingId": 4,
                    "jobTitle": "Financial Analyst",
                    "jobDescription": "z" * 300,
                    "postingStartTimestampUTC": "2026-08-14T00:00:00+00:00",
                },
            ],
            "maxCount": 4,
        }

    stub(search=search)
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())

    assert [job.external_id for job in result.jobs] == ["golden1:4"]


@pytest.mark.parametrize("body", [{}, {"jobPostings": "not-a-list"}, {"jobPostings": 5}])
async def test_an_unexpected_response_shape_is_a_parse_error(stub, body: dict) -> None:
    stub(search=lambda url, payload: body)
    portal = DayforcePortal({"boards": one_board()})

    with pytest.raises(PortalError):
        await portal.search(request())


async def test_an_empty_board_returns_no_jobs_rather_than_failing(stub) -> None:
    stub(search=lambda url, payload: {"jobPostings": [], "maxCount": 0})
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())

    assert result.jobs == []


async def test_non_dict_postings_are_ignored(stub) -> None:
    stub(
        search=lambda url, payload: {
            "jobPostings": ["nonsense", None, 42],
            "maxCount": 3,
        }
    )
    portal = DayforcePortal({"boards": one_board()})

    assert await portal.search(request()) is not None


@pytest.mark.parametrize(
    "error",
    [
        PortalTimeoutError("timed out", portal="dayforce"),
        PortalBlockedError("refused", portal="dayforce"),
        PortalRateLimitedError("slow down", portal="dayforce"),
        PortalParseError("bad json", portal="dayforce"),
    ],
)
async def test_every_board_failing_raises_rather_than_reporting_no_jobs(
    stub, error: PortalError
) -> None:
    """A portal that is down must be recorded as down, not as empty."""

    def search(url, payload):  # noqa: ANN001
        raise error

    stub(search=search)
    portal = DayforcePortal({"boards": one_board()})

    with pytest.raises(PortalError):
        await portal.search(request())


async def test_one_board_failing_does_not_lose_the_others(stub) -> None:
    def search(url, payload):  # noqa: ANN001
        if payload["clientNamespace"] == "broken":
            raise PortalError("board down", portal="dayforce")
        return {
            "jobPostings": [
                {
                    "jobPostingId": 7,
                    "jobTitle": "Financial Analyst",
                    "jobDescription": "w" * 300,
                    "postingStartTimestampUTC": "2026-08-14T00:00:00+00:00",
                }
            ],
            "maxCount": 1,
        }

    stub(search=search)
    portal = DayforcePortal(
        {
            "boards": [
                {"namespace": "broken", "board": "candidateportal", "company": "X"},
                {"namespace": "working", "board": "candidateportal", "company": "Y"},
            ]
        }
    )

    result = await portal.search(request())

    assert [job.external_id for job in result.jobs] == ["working:7"]


async def test_health_check_reports_rather_than_raises(stub) -> None:
    stub(search=lambda url, payload: (_ for _ in ()).throw(PortalError("down", portal="dayforce")))
    portal = DayforcePortal({"boards": one_board()})

    health = await portal.health_check()

    assert health.is_available is False
    assert health.detail


# --- URL classification ----------------------------------------------------


def test_a_dayforce_job_url_classifies_as_an_ats_link() -> None:
    """Dayforce is the employer's application system, so its links must rank
    above an aggregator's. ``dayforcehcm.com`` was already a known ATS host;
    this pins that the URLs this adapter builds are actually recognised."""
    url = DayforceBoard("golden1", "candidateportal", "Golden 1").job_url(52259)

    assert classify_url(url) is JobUrlKind.ATS


def test_the_company_is_configuration_not_a_guess() -> None:
    """A board configured without a company yields no company.

    The payload names no employer, so the alternative would be inventing one -
    and a job attributed to the wrong company can carry an application status
    with it.
    """
    board = DayforceBoard("acme", "careers", None)
    assert board.company is None


async def test_a_board_without_a_company_yields_no_company(stub) -> None:
    stub()
    portal = DayforceP = DayforcePortal({"boards": one_board(company=None)})

    result = await portal.search(request())

    assert result.jobs
    assert all(job.company is None for job in result.jobs)


# --- Description -----------------------------------------------------------


async def test_the_full_posting_text_arrives_with_the_listing(stub) -> None:
    stub()
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())
    job = result.jobs[0]

    # No second request is made to obtain it.
    assert await portal.fetch_description(job) == job.description
    assert job.description and len(job.description) > 500


async def test_a_posting_with_no_description_is_collected_and_left_to_scoring(
    stub,
) -> None:
    """Scoring rejects a posting it cannot read; the adapter does not pre-judge."""

    def search(url, payload):  # noqa: ANN001
        return {
            "jobPostings": [
                {
                    "jobPostingId": 8,
                    "jobTitle": "Financial Analyst",
                    "jobDescription": "",
                    "postingStartTimestampUTC": "2026-08-14T00:00:00+00:00",
                }
            ],
            "maxCount": 1,
        }

    stub(search=search)
    portal = DayforcePortal({"boards": one_board()})

    result = await portal.search(request())

    assert result.jobs[0].description is None


# --- The captured fixture --------------------------------------------------


def test_the_fixture_is_a_real_recorded_response(captured: dict[str, Any]) -> None:
    """Guards against the fixture being replaced by something hand-written."""
    board = captured["golden1/candidateportal"]

    assert set(board) >= {"jobPostings", "maxCount", "offset", "count"}
    posting = board["jobPostings"][0]
    assert set(posting) >= {
        "jobPostingId",
        "jobTitle",
        "jobDescription",
        "postingStartTimestampUTC",
        "postingLocations",
    }


def test_the_recorded_response_confirms_the_api_does_not_paginate(
    captured: dict[str, Any],
) -> None:
    """Why ``supports_pagination`` is False, recorded rather than asserted in prose."""
    for board in captured.values():
        assert board["offset"] == 0


# --- Through the existing pipeline -----------------------------------------
#
# The adapter is only half the requirement: Dayforce listings have to reach the
# same gates every other portal's listings pass. These tests run real search
# runs with the real DayforcePortal over a stubbed transport, so nothing here
# touches the network either.


from datetime import timedelta  # noqa: E402

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.constants import Seniority, SkillKind  # noqa: E402
from app.models.profile import ResumeSkill  # noqa: E402
from app.search.freshness import is_eligible_age  # noqa: E402
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

FINANCE_TEXT = """About the role
We are hiring a Financial Analyst to own forecasting and the monthly close.

Requirements:
- 3+ years of experience in financial analysis
- Advanced Excel
- SQL
- Forecasting and budgeting

Preferred qualifications:
- Power BI

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


def dayforce_payload(*, posted: datetime, count: int = 3, start_id: int = 500) -> dict:
    """A Dayforce response shaped exactly like the recorded one."""
    return {
        "jobPostings": [
            {
                "jobPostingId": start_id + index,
                "jobTitle": "Financial Analyst",
                "jobDescription": FINANCE_TEXT,
                "postingStartTimestampUTC": posted.isoformat(),
                "postingLocations": [
                    {
                        "formattedAddress": "Austin, TX, USA",
                        "isoCountryCode": "US",
                        "stateCode": "TX",
                        "cityName": "Austin",
                    }
                ],
            }
            for index in range(count)
        ],
        "maxCount": count,
        "offset": 0,
        "count": count,
    }


@pytest.fixture
def dayforce_live(monkeypatch: pytest.MonkeyPatch):
    """Drive the real adapter from a payload instead of the network."""

    def _apply(payload: dict):
        async def _get_json(self, url, *, params=None):  # noqa: ANN001
            return {"csrfToken": "test-token"}

        async def _post_json(self, url, *, payload_=None, **kwargs):  # noqa: ANN001
            return payload

        async def _post(self, url, *, payload, headers=None):  # noqa: ANN001
            return payload_holder["value"]

        payload_holder = {"value": payload}
        monkeypatch.setattr("app.portals.http.PortalHttpClient.get_json", _get_json)
        monkeypatch.setattr("app.portals.http.PortalHttpClient.post_json", _post)

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


async def test_dayforce_jobs_pass_the_normal_scoring_pipeline(
    db_session: AsyncSession, dayforce_live
) -> None:
    """A fresh, US, on-role Dayforce listing qualifies like any other."""
    dayforce_live(dayforce_payload(posted=NOW - timedelta(days=3)))
    _, resume = await analysed_resume(db_session)

    prepared, adapters = await build_run(
        db_session,
        resume=resume,
        portal_specs=[("dayforce", DayforcePortal({"boards": one_board()}))],
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
        # The locked threshold and the window applied, unchanged.
        assert item.final_score >= 70.0
        assert is_eligible_age(item.posted_at, NOW)
        assert item.preferred_application_url
        assert "dayforcehcm.com" in item.preferred_application_url


async def test_an_over_age_dayforce_job_is_excluded_by_the_existing_window(
    db_session: AsyncSession, dayforce_live
) -> None:
    """No special case: the 30-day rule rejects it exactly as it would any portal."""
    dayforce_live(dayforce_payload(posted=NOW - timedelta(days=45)))
    _, resume = await analysed_resume(db_session)

    prepared, adapters = await build_run(
        db_session,
        resume=resume,
        portal_specs=[("dayforce", DayforcePortal({"boards": one_board()}))],
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


async def test_a_non_us_dayforce_job_is_excluded_by_the_existing_us_rule(
    db_session: AsyncSession, dayforce_live
) -> None:
    payload = dayforce_payload(posted=NOW - timedelta(days=2))
    for posting in payload["jobPostings"]:
        posting["postingLocations"] = [
            {"formattedAddress": "Berlin, Germany", "isoCountryCode": "DE"}
        ]
    dayforce_live(payload)
    _, resume = await analysed_resume(db_session)

    prepared, adapters = await build_run(
        db_session,
        resume=resume,
        portal_specs=[("dayforce", DayforcePortal({"boards": one_board()}))],
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
    db_session: AsyncSession, dayforce_live
) -> None:
    dayforce_live(dayforce_payload(posted=NOW - timedelta(days=4)))
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
            ("dayforce", DayforcePortal({"boards": one_board()})),
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


async def test_adding_dayforce_does_not_alter_the_other_portal_results(
    db_session: AsyncSession, dayforce_live
) -> None:
    """The regression guard: the same search, with and without Dayforce.

    The existing portal's results must be identical - same jobs, same scores,
    same order. Dayforce adds to the pool; it does not disturb it.
    """
    dayforce_live(dayforce_payload(posted=NOW - timedelta(days=4)))
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
            ("dayforce", DayforcePortal({"boards": one_board()})),
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
    # And Dayforce genuinely contributed on top.
    assert together.returned_count > alone.returned_count
