"""The SmartRecruiters adapter, against pages captured from live boards.

``tests/fixtures/portals/smartrecruiters_pages.json`` holds real responses: a
board search with hits, the same board searched for something it does not have,
a slug that does not exist, an employer that has moved off the platform, a
United States job page and a non-United-States one. Parsing is tested against
those rather than against hand-written markup, so a change in SmartRecruiters'
templates shows up here as a failure rather than in production as a silent
zero.

Transport is stubbed throughout: no test in this file touches the network.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import ApplicationType, FreshnessWindow
from app.portals.adapters.smartrecruiters import (
    CAREERS_BASE,
    DEFAULT_BOARDS,
    FORBIDDEN_API_HOST,
    JOBS_BASE,
    MAX_SEARCH_PAGES,
    OPTED_OUT_EMPLOYERS,
    PAGINATION_THRESHOLD,
    SmartRecruitersPortal,
    apply_url,
    build_description,
    clean_text,
    company_identifier,
    employment_type,
    format_location,
    html_to_text,
    is_opted_out,
    is_us_posting,
    job_details,
    job_identity,
    job_title,
    meta_itemprop,
    organization_name,
    parse_cards,
    parse_date_posted,
    title_matches,
    title_tokens,
)
from app.portals.base import PortalSearchRequest, RawJob
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.processing.titles import detect_work_mode
from app.processing.urls import JobUrlKind, classify_url
from app.processing.us_locations import classify_us_location
from app.search.freshness import is_eligible_age

FIXTURE = Path(__file__).parent / "fixtures" / "portals" / "smartrecruiters_pages.json"
PAGES: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))

SEARCH_HITS: str = PAGES["search_hits"]["html"]
SEARCH_EMPTY: str = PAGES["search_empty"]["html"]
INVALID_SLUG: str = PAGES["invalid_slug"]["html"]
REDIRECT_AWAY: str = PAGES["redirect_away"]["html"]
JOB_US: str = PAGES["job_us"]["html"]
JOB_NON_US: str = PAGES["job_non_us"]["html"]
JOB_US_URL: str = PAGES["job_us"]["url"]

BOARD = "WellmarkInc"


# --------------------------------------------------------------------------
# A stub transport
# --------------------------------------------------------------------------


class FakeClient:
    """Stands in for ``PortalHttpClient``, recording every request made.

    The request log is the point: several tests here assert on *how many*
    requests a collection cost, which is the property that stops synonymous
    query variations from multiplying into repeated job-page fetches.
    """

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        *,
        default: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.pages = pages or {}
        self.default = default
        self.error = error
        self.requests: list[str] = []

    async def get_text(self, url: str, **_: Any) -> str:
        self.requests.append(url)
        if self.error is not None:
            raise self.error
        if url in self.pages:
            return self.pages[url]
        for key, body in self.pages.items():
            if url.startswith(key):
                return body
        if self.default is not None:
            return self.default
        raise PortalError(f"no page for {url}", portal="smartrecruiters")

    async def head_ok(self, url: str) -> bool:
        self.requests.append(f"HEAD {url}")
        return True


def build_portal(
    boards: list[str] | None = None,
    *,
    careers: FakeClient | None = None,
    jobs: FakeClient | None = None,
    options: dict[str, Any] | None = None,
) -> tuple[SmartRecruitersPortal, FakeClient, FakeClient]:
    config = dict(options or {})
    config.setdefault("boards", boards if boards is not None else [BOARD])
    portal = SmartRecruitersPortal(config)
    careers_client = careers or FakeClient()
    jobs_client = jobs or FakeClient()
    portal._careers = careers_client  # type: ignore[assignment]
    portal._jobs = jobs_client  # type: ignore[assignment]
    return portal, careers_client, jobs_client


def request_for(*queries: str, limit: int = 100) -> PortalSearchRequest:
    return PortalSearchRequest(
        queries=queries, window=FreshnessWindow.LAST_30D, limit=limit
    )


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


def board_page(slug: str, cards: list[tuple[str, str]]) -> str:
    """A board page carrying its own identifier and the given cards."""
    rendered = "".join(
        f'<li class="opening-job"><a href="{url}" class="link--block details '
        f'js-job-ad-link"><h4 class="details-title job-title">{title}</h4>'
        f'<p class="details-desc job-desc"><span>Somewhere</span></p></a></li>'
        for url, title in cards
    )
    return (
        f"<html><body><ul>{rendered}</ul>"
        f'<script>var CAREERPAGE = {{ "companyIdentifier": "{slug}" }};</script>'
        f"</body></html>"
    )


def job_page(
    *,
    title: str = "Financial Analyst",
    company: str = "Fixture Co",
    posted: str | None = None,
    country: str = "United States",
    locality: str = "Des Moines",
    region: str = "Iowa",
    emp_type: str | None = "Full-time",
    details: str = "",
    apply_to: str | None = "https://jobs.smartrecruiters.com/external-referrals/company/Fixture/publication/abc",
    body: str = "<p>Responsibilities and qualifications.</p>",
) -> str:
    when = posted if posted is not None else datetime.now(UTC).isoformat()
    when_meta = (
        f'<meta itemprop="datePosted" content="{when}">' if posted != "" else ""
    )
    type_li = (
        f'<li class="job-detail" itemprop="employmentType">{emp_type}</li>'
        if emp_type
        else ""
    )
    apply_html = f'<a href="{apply_to}">Apply</a>' if apply_to else ""
    return (
        "<html><body>"
        f"<h1>{title}</h1>"
        f'<ul>{type_li}{details}</ul>'
        f'<div itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">'
        f'<div itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">'
        f'<meta itemprop="addressCountry" content="{country}">'
        f'<meta itemprop="addressLocality" content="{locality}">'
        f'<meta itemprop="addressRegion" content="{region}">'
        f"</div></div>"
        f'<div itemprop="hiringOrganization" itemscope itemtype="http://schema.org/Organization">'
        f'<meta itemprop="name" content="{company}">'
        f"</div>"
        f"{when_meta}"
        f'<article><div class="job-sections">'
        f'<section class="job-section"><h3>Job Description</h3>'
        f'<div class="wysiwyg">{body}</div></section>'
        f"</div></article>"
        f"{apply_html}"
        "</body></html>"
    )


# --------------------------------------------------------------------------
# Registration, capabilities, configuration
# --------------------------------------------------------------------------


def test_the_adapter_is_registered_under_its_slug() -> None:
    import app.portals.adapters  # noqa: F401  - registers on import
    from app.portals.registry import registry

    assert registry.is_registered("smartrecruiters")
    assert registry.get("smartrecruiters") is SmartRecruitersPortal


def test_slug_and_display_name() -> None:
    assert SmartRecruitersPortal.slug == "smartrecruiters"
    assert SmartRecruitersPortal.display_name == "SmartRecruiters"


def test_capabilities_state_what_smartrecruiters_actually_does() -> None:
    capabilities = SmartRecruitersPortal().capabilities

    assert capabilities.supports_date_filter is False
    # `search` already returns the full text from the job page it opens.
    assert capabilities.supports_description_fetch is False
    assert capabilities.supports_pagination is True
    assert capabilities.requires_auth is False
    assert capabilities.provides_application_url is True


def test_an_unconfigured_portal_reads_the_verified_defaults() -> None:
    assert SmartRecruitersPortal().boards == DEFAULT_BOARDS


def test_configuration_replaces_the_defaults_entirely() -> None:
    """Adding an employer must never require a deployment."""
    assert SmartRecruitersPortal({"boards": ["Acme"]}).boards == ("Acme",)


def test_a_single_board_string_is_accepted() -> None:
    assert SmartRecruitersPortal({"boards": "Acme"}).boards == ("Acme",)


def test_the_mapping_form_is_accepted_too() -> None:
    """An operator copying the Lever or Workday shape is not silently ignored."""
    portal = SmartRecruitersPortal({"boards": [{"slug": "Acme"}]})

    assert portal.boards == ("Acme",)


def test_concurrency_is_configurable_and_never_zero() -> None:
    assert SmartRecruitersPortal({"max_concurrent_jobs": 3})._max_concurrent == 3
    assert SmartRecruitersPortal({"max_concurrent_jobs": 0})._max_concurrent == 1
    assert SmartRecruitersPortal({"max_concurrent_jobs": "lots"})._max_concurrent >= 1


# --------------------------------------------------------------------------
# The API host, and the employer opt-out list
# --------------------------------------------------------------------------


def test_the_adapter_builds_no_url_against_the_forbidden_api() -> None:
    """The one-request API is off limits and must stay that way.

    Scanned at the AST level over string literals with docstrings removed, so
    the module docstring stays free to explain *why* the API is avoided while
    any literal that could actually address it fails this test.
    """
    import ast

    source = (
        Path(__file__).parent.parent
        / "app"
        / "portals"
        / "adapters"
        / "smartrecruiters.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    docstrings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]

    for literal in literals:
        assert "//api.smartrecruiters.com" not in literal
        assert not literal.startswith("https://api.")
        assert "/v1/companies/" not in literal

    # Named exactly once, as the constant the tests assert against.
    assert sum(1 for value in literals if value == FORBIDDEN_API_HOST) == 1


def test_every_url_the_adapter_builds_is_on_a_permitted_host() -> None:
    portal = SmartRecruitersPortal()

    for slug in portal.boards:
        for url in (portal.board_url(slug), portal.search_url(slug, "analyst")):
            assert url.startswith(CAREERS_BASE)
            assert FORBIDDEN_API_HOST not in url


def test_a_search_never_contacts_the_api_host() -> None:
    careers = FakeClient({f"{CAREERS_BASE}/{BOARD}": board_page(BOARD, [])})
    portal, careers_client, jobs_client = build_portal(careers=careers)

    run(portal.search(request_for("financial analyst")))

    for url in careers_client.requests + jobs_client.requests:
        assert FORBIDDEN_API_HOST not in url
        assert url.startswith(CAREERS_BASE) or url.startswith(JOBS_BASE)


def test_the_opt_out_list_is_the_one_the_vendor_publishes() -> None:
    assert len(OPTED_OUT_EMPLOYERS) == 68
    for known in ("AveryDennison", "BoschGroup", "Pluralsight", "CityofPhiladelphia"):
        assert known in OPTED_OUT_EMPLOYERS


def test_the_products_own_paths_are_not_mistaken_for_employers() -> None:
    """The live file has 71 slug-shaped rules; three are not companies."""
    for not_an_employer in ("a", "referrals-portal", "web-sso"):
        assert not_an_employer not in OPTED_OUT_EMPLOYERS


@pytest.mark.parametrize(
    "slug", ["AveryDennison", "averydennison", "AVERYDENNISON", " AveryDennison "]
)
def test_an_opted_out_employer_is_recognised_however_it_is_written(slug: str) -> None:
    assert is_opted_out(slug) is True


@pytest.mark.parametrize("slug", DEFAULT_BOARDS)
def test_no_shipped_board_is_an_opted_out_employer(slug: str) -> None:
    assert is_opted_out(slug) is False


def test_configuring_an_opted_out_employer_drops_it() -> None:
    portal = SmartRecruitersPortal({"boards": ["WellmarkInc", "AveryDennison"]})

    assert portal.boards == ("WellmarkInc",)


def test_an_opted_out_employer_generates_no_request_at_all() -> None:
    """The assertion that matters: not merely absent from results, unrequested."""
    careers = FakeClient(default=board_page("AveryDennison", []))
    portal, careers_client, jobs_client = build_portal(
        ["AveryDennison"], careers=careers
    )

    with pytest.raises(PortalError, match="No SmartRecruiters boards are configured"):
        run(portal.search(request_for("financial analyst")))

    assert careers_client.requests == []
    assert jobs_client.requests == []


def test_a_permitted_employer_alongside_an_opted_out_one_is_still_read() -> None:
    careers = FakeClient({f"{CAREERS_BASE}/{BOARD}": board_page(BOARD, [])})
    portal, careers_client, _ = build_portal(
        ["AveryDennison", BOARD], careers=careers
    )

    run(portal.search(request_for("financial analyst")))

    assert len(careers_client.requests) == 1
    assert all("AveryDennison" not in url for url in careers_client.requests)


# --------------------------------------------------------------------------
# URL construction
# --------------------------------------------------------------------------


def test_the_search_url_is_the_documented_shape() -> None:
    portal = SmartRecruitersPortal()

    assert portal.search_url("Acme", "financial analyst") == (
        f"{CAREERS_BASE}/Acme?search=financial%20analyst"
    )


def test_the_search_url_escapes_awkward_queries() -> None:
    portal = SmartRecruitersPortal()
    url = portal.search_url("Acme", "FP&A Analyst")

    assert "&search=" not in url
    assert "FP%26A" in url


def test_pagination_is_expressed_as_a_page_parameter() -> None:
    portal = SmartRecruitersPortal()

    assert portal.search_url("Acme", "analyst", 1).endswith("?search=analyst")
    assert portal.search_url("Acme", "analyst", 2).endswith("?search=analyst&page=2")


# --------------------------------------------------------------------------
# Board validity: the soft failures
# --------------------------------------------------------------------------


def test_a_real_board_identifies_itself() -> None:
    assert company_identifier(SEARCH_HITS) == BOARD


def test_a_board_with_no_matching_openings_still_identifies_itself() -> None:
    """Empty success: the employer exists and has nothing for this query."""
    assert company_identifier(SEARCH_EMPTY) == BOARD
    assert parse_cards(SEARCH_EMPTY) == []


def test_a_slug_that_does_not_exist_carries_no_identifier() -> None:
    assert company_identifier(INVALID_SLUG) is None


def test_an_employer_that_moved_off_the_platform_carries_no_identifier() -> None:
    """BoschGroup answers 200 with jobs.bosch.com. Status proves nothing."""
    assert company_identifier(REDIRECT_AWAY) is None


def test_an_empty_board_is_an_empty_success_not_a_failure() -> None:
    careers = FakeClient({f"{CAREERS_BASE}/{BOARD}": SEARCH_EMPTY})
    portal, _, jobs_client = build_portal(careers=careers)

    result = run(portal.search(request_for("financial analyst")))

    assert result.jobs == []
    assert jobs_client.requests == []


def test_an_invalid_slug_is_reported_as_an_unavailable_board() -> None:
    careers = FakeClient(default=INVALID_SLUG)
    portal, _, _ = build_portal(careers=careers)

    with pytest.raises(PortalError, match="did not answer as itself"):
        run(portal._search_board(BOARD, "financial analyst"))


def test_an_employer_redirecting_away_is_reported_as_unavailable() -> None:
    careers = FakeClient(default=REDIRECT_AWAY)
    portal, _, _ = build_portal(careers=careers)

    with pytest.raises(PortalError, match="did not answer as itself"):
        run(portal._search_board(BOARD, "financial analyst"))


def test_a_board_answering_as_a_different_employer_is_refused() -> None:
    """A redirect to another SmartRecruiters company must not be accepted."""
    careers = FakeClient(default=board_page("SomebodyElse", []))
    portal, _, _ = build_portal(careers=careers)

    with pytest.raises(PortalError, match="did not answer as itself"):
        run(portal._search_board(BOARD, "financial analyst"))


def test_the_identifier_match_is_case_insensitive() -> None:
    careers = FakeClient(default=board_page("wellmarkinc", []))
    portal, _, _ = build_portal(careers=careers)

    assert run(portal.search(request_for("financial analyst"))).jobs == []


# --------------------------------------------------------------------------
# Card parsing and the title gate
# --------------------------------------------------------------------------


def test_cards_are_read_from_a_real_search_page() -> None:
    cards = parse_cards(SEARCH_HITS)

    assert len(cards) == 14
    for url, title in cards:
        assert url.startswith(f"{JOBS_BASE}/{BOARD}/")
        assert title


def test_a_posting_rendered_under_two_departments_counts_once() -> None:
    duplicate = ("https://jobs.smartrecruiters.com/X/1234567-a", "Financial Analyst")
    page = board_page("X", [duplicate, duplicate, duplicate])

    assert len(parse_cards(page)) == 1


def test_a_page_with_no_cards_parses_to_nothing_rather_than_failing() -> None:
    assert parse_cards("<html><body>nothing here</body></html>") == []


def test_malformed_markup_does_not_raise() -> None:
    assert parse_cards("<a href=") == []
    assert company_identifier("<<<>>>") is None
    assert job_title("<h1>") is None


TITLE_CASES = [
    ("Financial Analyst", "Senior Financial Analyst", True),
    ("Financial Analyst", "Financial Analyst, Business F&S", True),
    ("Financial Analyst", "Business Analyst", False),
    ("Data Analyst", "Data Analyst II", True),
    ("Data Analyst", "Data Engineer", False),
    ("FP&A Analyst", "FP&A Analyst, Corporate", True),
    ("FP&A Analyst", "Financial Analyst", False),
    ("Analyst", "Senior Data Analyst", True),
    ("", "Anything", False),
]


@pytest.mark.parametrize(("query", "title", "expected"), TITLE_CASES)
def test_title_matching_is_unchanged(query: str, title: str, expected: bool) -> None:
    assert title_matches(query, title) is expected


@pytest.mark.parametrize(("query", "title", "expected"), TITLE_CASES)
def test_title_matching_agrees_with_greenhouse(
    query: str, title: str, expected: bool
) -> None:
    """The rule is copied per adapter by design; the copies must not drift."""
    from app.portals.adapters.greenhouse import title_matches as greenhouse_matches

    assert title_matches(query, title) is greenhouse_matches(query, title)


def test_single_letters_are_not_significant_tokens() -> None:
    assert "a" not in title_tokens("FP&A Analyst")
    assert {"fp", "analyst"} <= title_tokens("FP&A Analyst")


def test_the_fuzzy_search_over_returning_is_handled_locally() -> None:
    """SmartRecruiters answers "data analyst" with "Data Wrangler".

    The server-side search is a discovery mechanism, not a filter. Only the
    titles that pass the same-role gate may cost a job-page request.
    """
    cards = [
        ("https://jobs.smartrecruiters.com/X/1000001-a", "Data Analyst"),
        ("https://jobs.smartrecruiters.com/X/1000002-b", "Data Wrangler"),
        ("https://jobs.smartrecruiters.com/X/1000003-c", "Team Leader - Business Analysis"),
        ("https://jobs.smartrecruiters.com/X/1000004-d", "Warehouse Associate"),
    ]
    careers = FakeClient(default=board_page("X", cards))
    jobs = FakeClient(default=job_page(title="Data Analyst"))
    portal, _, jobs_client = build_portal(["X"], careers=careers, jobs=jobs)

    result = run(portal.search(request_for("data analyst")))

    assert len(result.jobs) == 1
    assert jobs_client.requests == ["https://jobs.smartrecruiters.com/X/1000001-a"]


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def test_a_normal_search_costs_exactly_one_request() -> None:
    """The search returns its whole result set on one page."""
    careers = FakeClient(default=board_page("X", [("https://jobs.smartrecruiters.com/X/1-a", "Analyst")]))
    jobs = FakeClient(default=job_page(title="Analyst"))
    portal, careers_client, _ = build_portal(["X"], careers=careers, jobs=jobs)

    run(portal.search(request_for("analyst")))

    assert len(careers_client.requests) == 1
    assert "page=" not in careers_client.requests[0]


def test_an_implausibly_full_first_page_asks_for_a_second() -> None:
    first = [
        (f"https://jobs.smartrecruiters.com/X/{1_000_000 + i}-a", "Analyst")
        for i in range(PAGINATION_THRESHOLD)
    ]
    second = [("https://jobs.smartrecruiters.com/X/2000001-b", "Analyst")]
    careers = FakeClient(
        {
            f"{CAREERS_BASE}/X?search=analyst&page=2": board_page("X", second),
            f"{CAREERS_BASE}/X?search=analyst": board_page("X", first),
        }
    )
    jobs = FakeClient(default=job_page(title="Analyst"))
    portal, careers_client, _ = build_portal(
        ["X"], careers=careers, jobs=jobs, options={"max_concurrent_jobs": 4}
    )

    run(portal.search(request_for("analyst", limit=500)))

    assert any("page=2" in url for url in careers_client.requests)


def test_pagination_stops_when_a_page_adds_nothing_new() -> None:
    full = [
        (f"https://jobs.smartrecruiters.com/X/{1_000_000 + i}-a", "Analyst")
        for i in range(PAGINATION_THRESHOLD)
    ]
    careers = FakeClient(default=board_page("X", full))
    jobs = FakeClient(default=job_page(title="Analyst"))
    portal, careers_client, _ = build_portal(
        ["X"], careers=careers, jobs=jobs, options={"max_concurrent_jobs": 4}
    )

    run(portal.search(request_for("analyst", limit=500)))

    # Page 2 repeats page 1, so the walk stops rather than running to the cap.
    assert len(careers_client.requests) == 2
    assert len(careers_client.requests) < MAX_SEARCH_PAGES + 1


def test_one_employer_cannot_cause_unbounded_traversal() -> None:
    pages = {}
    for page in range(1, 12):
        cards = [
            (f"https://jobs.smartrecruiters.com/X/{page}{i:06d}-a", "Analyst")
            for i in range(PAGINATION_THRESHOLD)
        ]
        suffix = "" if page == 1 else f"&page={page}"
        pages[f"{CAREERS_BASE}/X?search=analyst{suffix}"] = board_page("X", cards)
    careers = FakeClient(dict(sorted(pages.items(), key=lambda kv: -len(kv[0]))))
    jobs = FakeClient(default=job_page(title="Analyst"))
    portal, careers_client, _ = build_portal(
        ["X"], careers=careers, jobs=jobs, options={"max_concurrent_jobs": 4}
    )

    run(portal.search(request_for("analyst", limit=5000)))

    assert len(careers_client.requests) <= MAX_SEARCH_PAGES


# --------------------------------------------------------------------------
# Request economy
# --------------------------------------------------------------------------


def test_one_posting_answering_several_variations_costs_one_job_fetch() -> None:
    """The regression this portal most needs guarding.

    The collector calls ``search`` once per same-role variation, and the fuzzy
    server-side search returns overlapping cards for synonymous queries. A
    posting that answers three of them must still be fetched once.
    """
    card = [("https://jobs.smartrecruiters.com/X/1234567-a", "Senior Financial Analyst")]
    careers = FakeClient(default=board_page("X", card))
    jobs = FakeClient(default=job_page(title="Senior Financial Analyst"))
    portal, careers_client, jobs_client = build_portal(["X"], careers=careers, jobs=jobs)

    for query in ("financial analyst", "senior financial analyst", "finance analyst"):
        run(portal.search(request_for(query)))

    # One search per distinct query, because the queries differ.
    assert len(careers_client.requests) == 3
    # But exactly one job page, because it is the same posting.
    assert len(jobs_client.requests) == 1


def test_overlapping_variations_within_one_call_also_fetch_once() -> None:
    card = [("https://jobs.smartrecruiters.com/X/1234567-a", "Senior Financial Analyst")]
    careers = FakeClient(default=board_page("X", card))
    jobs = FakeClient(default=job_page(title="Senior Financial Analyst"))
    portal, _, jobs_client = build_portal(["X"], careers=careers, jobs=jobs)

    result = run(
        portal.search(request_for("financial analyst", "senior financial analyst"))
    )

    assert len(result.jobs) == 1
    assert len(jobs_client.requests) == 1


def test_a_search_page_is_fetched_once_per_board_and_query() -> None:
    careers = FakeClient(default=board_page("X", []))
    portal, careers_client, _ = build_portal(["X"], careers=careers)

    run(portal.search(request_for("analyst")))
    run(portal.search(request_for("analyst")))

    assert len(careers_client.requests) == 1


def test_the_same_posting_on_two_boards_is_fetched_once() -> None:
    shared = [("https://jobs.smartrecruiters.com/X/1234567-a", "Financial Analyst")]
    careers = FakeClient(
        {
            f"{CAREERS_BASE}/A": board_page("A", shared),
            f"{CAREERS_BASE}/B": board_page("B", shared),
        }
    )
    jobs = FakeClient(default=job_page())
    portal, _, jobs_client = build_portal(["A", "B"], careers=careers, jobs=jobs)

    run(portal.search(request_for("financial analyst")))

    assert len(jobs_client.requests) == 1


def test_the_result_limit_bounds_how_many_job_pages_are_opened() -> None:
    cards = [
        (f"https://jobs.smartrecruiters.com/X/{1_000_000 + i}-a", "Financial Analyst")
        for i in range(20)
    ]
    careers = FakeClient(default=board_page("X", cards))
    jobs = FakeClient(default=job_page())
    portal, _, jobs_client = build_portal(["X"], careers=careers, jobs=jobs)

    result = run(portal.search(request_for("financial analyst", limit=5)))

    assert len(jobs_client.requests) == 5
    assert len(result.jobs) == 5


# --------------------------------------------------------------------------
# Job page parsing
# --------------------------------------------------------------------------


def test_a_live_job_page_maps_onto_the_raw_job_contract() -> None:
    portal, _, _ = build_portal()

    job = portal._to_raw_job(JOB_US, url=JOB_US_URL)

    assert isinstance(job, RawJob)
    assert job.source == "smartrecruiters"
    assert job.title == "Sr. Financial Analyst - Finance & Management Reporting"
    assert job.company == "Wellmark, Inc."
    assert job.external_id == "744000147309143"
    assert job.source_url == JOB_US_URL
    assert job.location == "Des Moines, Iowa, United States"
    assert job.employment_type == "Full-time"
    assert job.posted_at == datetime(2026, 9, 3, 16, 39, 32, 926000, tzinfo=UTC)
    assert job.application_url and "external-referrals" in job.application_url
    assert job.description and len(job.description) > 5000


def test_the_external_id_is_the_numeric_job_id() -> None:
    assert job_identity(JOB_US_URL) == (BOARD, "744000147309143")


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.smartrecruiters.com/Acme/",
        "https://jobs.smartrecruiters.com/Acme/not-numeric-slug",
        "https://jobs.smartrecruiters.com/",
        "nonsense",
    ],
)
def test_a_url_with_no_numeric_id_yields_no_identity(url: str) -> None:
    assert job_identity(url) is None


def test_a_posting_whose_url_carries_no_id_is_dropped() -> None:
    portal, _, _ = build_portal()

    assert portal._to_raw_job(JOB_US, url="https://jobs.smartrecruiters.com/X/abc") is None


def test_the_company_comes_from_the_page_not_from_configuration() -> None:
    assert organization_name(JOB_US) == "Wellmark, Inc."


def test_the_company_is_read_from_the_organisation_scope() -> None:
    """``itemprop="name"`` appears more than once; the first is not the company."""
    page = (
        '<meta itemprop="name" content="Some Place">'
        '<div itemprop="hiringOrganization" itemscope>'
        '<meta itemprop="name" content="Real Employer">'
        "</div>"
    )

    assert organization_name(page) == "Real Employer"


def test_the_company_falls_back_to_the_social_metadata() -> None:
    page = '<meta property="og:site_name" content="Sandisk">'

    assert organization_name(page) == "Sandisk"


def test_the_application_url_is_read_never_constructed() -> None:
    assert apply_url(JOB_US) == (
        "https://jobs.smartrecruiters.com/external-referrals/company/WellmarkInc"
        "/publication/ade859f5-cdb9-4e57-a77c-063eda2590ea?dcr_ci=WellmarkInc"
    )


def test_a_posting_with_no_published_apply_link_gets_none() -> None:
    portal, _, _ = build_portal()

    job = portal._to_raw_job(job_page(apply_to=None), url="https://jobs.smartrecruiters.com/X/1234567-a")

    assert job is not None
    assert job.application_url is None


def test_the_application_type_is_declared() -> None:
    portal, _, _ = build_portal()

    job = portal._to_raw_job(JOB_US, url=JOB_US_URL)

    assert job is not None
    assert job.raw_payload["application_type"] == ApplicationType.LONG_APPLICATION.value


def test_a_posting_with_no_title_is_dropped() -> None:
    portal, _, _ = build_portal()
    page = JOB_US.replace("<h1", "<div", 1).replace("</h1>", "</div>", 1)
    page = re.sub(r"<h1[^>]*>.*?</h1>", "", page, flags=re.S)

    assert portal._to_raw_job(page, url=JOB_US_URL) is None


def test_a_posting_with_no_description_is_dropped() -> None:
    portal, _, _ = build_portal()
    page = re.sub(r'<section[^>]*class="[^"]*job-section[^"]*".*?</section>', "", JOB_US, flags=re.S)
    page = re.sub(r'<div[^>]*class="[^"]*job-sections[^"]*".*?</article>', "", page, flags=re.S)

    assert build_description(page) is None
    assert portal._to_raw_job(page, url=JOB_US_URL) is None


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


def test_date_posted_is_read_from_the_microdata() -> None:
    assert meta_itemprop(JOB_US, "datePosted") == "2026-09-03T16:39:32.926Z"
    assert parse_date_posted("2026-09-03T16:39:32.926Z") == datetime(
        2026, 9, 3, 16, 39, 32, 926000, tzinfo=UTC
    )


def test_a_parsed_date_is_always_timezone_aware() -> None:
    parsed = parse_date_posted("2026-09-03T16:39:32.926Z")

    assert parsed is not None and parsed.tzinfo is not None


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "not-a-date", "03/09/2026", 20260903, [], {}, True, "2026-13-01"],
)
def test_a_missing_or_malformed_date_yields_none(value: Any) -> None:
    assert parse_date_posted(value) is None


def test_a_posting_without_a_date_is_dropped_rather_than_dated() -> None:
    portal, _, _ = build_portal()
    page = JOB_US.replace('itemprop="datePosted"', 'itemprop="notTheDate"')

    assert portal._to_raw_job(page, url=JOB_US_URL) is None


def test_a_posting_with_a_malformed_date_is_dropped() -> None:
    portal, _, _ = build_portal()
    page = re.sub(
        r'(<meta[^>]*itemprop="datePosted"[^>]*content=")[^"]*"',
        r"\1rubbish\"",
        JOB_US,
    )

    assert portal._to_raw_job(page, url=JOB_US_URL) is None


def test_the_update_timestamp_is_never_read_on_its_own() -> None:
    """Both fields are published; only datePosted may date a listing."""
    portal, _, _ = build_portal()
    page = JOB_US.replace('itemprop="datePosted"', 'itemprop="ignored"')

    # og:updated_time is still present and carries a perfectly good timestamp.
    assert meta_itemprop(page, "og:updated_time")
    assert portal._to_raw_job(page, url=JOB_US_URL) is None


def test_nothing_substitutes_the_current_time_for_a_publication_date() -> None:
    portal, _, _ = build_portal()
    page = JOB_US.replace('itemprop="datePosted"', 'itemprop="ignored"')

    assert portal._to_raw_job(page, url=JOB_US_URL) is None


def test_posted_at_feeds_the_real_freshness_gate() -> None:
    now = datetime(2026, 9, 15, tzinfo=UTC)

    assert is_eligible_age(parse_date_posted("2026-09-10T00:00:00Z"), now) is True
    assert is_eligible_age(parse_date_posted("2026-08-16T00:00:00Z"), now) is True
    assert is_eligible_age(parse_date_posted("2026-08-15T00:00:00Z"), now) is False
    assert is_eligible_age(None, now) is False


def test_a_posting_outside_the_window_still_maps_and_is_left_to_the_gate() -> None:
    """The adapter does not apply freshness; it supplies an accurate date."""
    portal, _, _ = build_portal()
    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()

    job = portal._to_raw_job(
        job_page(posted=old), url="https://jobs.smartrecruiters.com/X/1234567-a"
    )

    assert job is not None
    assert is_eligible_age(job.posted_at, datetime.now(UTC)) is False


# --------------------------------------------------------------------------
# Description
# --------------------------------------------------------------------------


def test_the_description_is_the_full_published_text() -> None:
    text = build_description(JOB_US)

    assert text is not None
    assert len(text) > 5000


def test_the_description_keeps_the_published_sections() -> None:
    text = build_description(JOB_US)

    assert text is not None
    for heading in ("Company Description", "Job Description", "Qualifications"):
        assert heading in text


def test_the_description_is_not_truncated() -> None:
    text = build_description(JOB_US)
    plain = html_to_text(JOB_US)

    assert text is not None and plain is not None
    words = [w for w in plain.split() if len(w) > 9]
    present = sum(1 for word in words if word in text)
    assert present >= len(words) * 0.5


def test_list_items_survive_as_bullets() -> None:
    text = html_to_text("<ul><li>One</li><li>Two</li></ul>")

    assert text is not None
    assert "- One" in text and "- Two" in text


def test_entities_are_decoded() -> None:
    assert html_to_text("<p>Salary &amp; benefits &lt;competitive&gt;</p>") == (
        "Salary & benefits <competitive>"
    )


@pytest.mark.parametrize("value", [None, "", "   ", "<p></p>"])
def test_an_empty_fragment_is_none_not_an_empty_string(value: Any) -> None:
    assert html_to_text(value) is None


def test_a_page_with_no_sections_yields_no_description() -> None:
    assert build_description("<html><body><h1>Title</h1></body></html>") is None


# --------------------------------------------------------------------------
# Employment type and work mode
# --------------------------------------------------------------------------


def test_employment_type_comes_from_its_own_itemprop() -> None:
    assert employment_type(JOB_US) == "Full-time"


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("Full-time", "Full-time"),
        ("full time", "Full-time"),
        ("Part-time", "Part-time"),
        ("Intern", "Intern"),
        ("Contract", "Contract"),
        ("Something Else", "Something Else"),
    ],
)
def test_employment_type_is_spelled_the_dashboards_way(
    published: str, expected: str
) -> None:
    page = f'<li class="job-detail" itemprop="employmentType">{published}</li>'

    assert employment_type(page) == expected


def test_employment_type_is_optional() -> None:
    assert employment_type("<html></html>") is None

    portal, _, _ = build_portal()
    job = portal._to_raw_job(
        job_page(emp_type=None), url="https://jobs.smartrecruiters.com/X/1234567-a"
    )
    assert job is not None
    assert job.employment_type is None


def test_the_employers_own_detail_chips_are_kept() -> None:
    details = job_details(JOB_US)

    assert "Work Environment: Hybrid" in details
    # The employment type has its own field and is not repeated here.
    assert "Full-time" not in details


def test_a_published_work_environment_reaches_the_shared_detector() -> None:
    from app.core.constants import WorkMode

    text = build_description(JOB_US)

    assert text is not None
    assert detect_work_mode(text[:2000]) is WorkMode.HYBRID


def test_a_posting_that_says_nothing_about_work_mode_claims_nothing() -> None:
    text = build_description(job_page(details=""))

    assert text is not None
    assert detect_work_mode(text[:2000]) is None


# --------------------------------------------------------------------------
# Location and the United States decision
# --------------------------------------------------------------------------


def test_the_location_is_built_from_the_structured_address() -> None:
    assert format_location(JOB_US) == "Des Moines, Iowa, United States"


def test_a_real_united_states_posting_classifies_as_united_states() -> None:
    assert classify_us_location(format_location(JOB_US)).is_us is True


def test_a_real_foreign_posting_does_not() -> None:
    location = format_location(JOB_NON_US)

    assert location is not None
    assert is_us_posting(JOB_NON_US) is False
    assert classify_us_location(location).is_us is False


@pytest.mark.parametrize(
    "country", ["US", "USA", "United States", "United States of America", "u.s."]
)
def test_every_spelling_of_the_united_states_is_accepted(country: str) -> None:
    page = f'<meta itemprop="addressCountry" content="{country}">'

    assert is_us_posting(page) is True


@pytest.mark.parametrize("country", ["Canada", "India", "Australia", "Poland"])
def test_other_countries_are_not_the_united_states(country: str) -> None:
    page = f'<meta itemprop="addressCountry" content="{country}">'

    assert is_us_posting(page) is False


def test_a_posting_with_no_country_reads_as_unknown() -> None:
    assert is_us_posting("<html></html>") is False
    assert format_location("<html></html>") is None


def test_a_partial_address_still_produces_what_it_has() -> None:
    page = '<meta itemprop="addressCountry" content="United States">'

    assert format_location(page) == "United States"


def test_the_board_card_is_never_the_country_signal() -> None:
    """Some employers render the employment type where others put a location.

    Reading a country off the card would mean reading "Full-time" as a place,
    so the structured job-page address is the only source.
    """
    portal, _, _ = build_portal()
    job = portal._to_raw_job(JOB_US, url=JOB_US_URL)

    assert job is not None
    assert job.location == "Des Moines, Iowa, United States"
    assert "Full-time" not in (job.location or "")


def test_the_adapter_does_not_filter_by_country_itself() -> None:
    """The pipeline decides; the adapter formats."""
    portal, _, _ = build_portal()

    job = portal._to_raw_job(
        JOB_NON_US, url="https://jobs.smartrecruiters.com/Sodexo/1234567-a"
    )

    assert job is not None
    assert classify_us_location(job.location).is_us is False


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_one_board_failing_does_not_cost_the_others() -> None:
    careers = FakeClient(
        {
            f"{CAREERS_BASE}/Good": board_page(
                "Good", [("https://jobs.smartrecruiters.com/Good/1234567-a", "Financial Analyst")]
            ),
            f"{CAREERS_BASE}/Bad": INVALID_SLUG,
        }
    )
    jobs = FakeClient(default=job_page())
    portal, _, _ = build_portal(["Bad", "Good"], careers=careers, jobs=jobs)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1


def test_every_board_failing_is_a_portal_error() -> None:
    careers = FakeClient(default=INVALID_SLUG)
    portal, _, _ = build_portal(["A", "B"], careers=careers)

    with pytest.raises(PortalError, match="No SmartRecruiters board could be read"):
        run(portal.search(request_for("financial analyst")))


def test_a_board_with_no_configured_employers_raises() -> None:
    portal = SmartRecruitersPortal()
    portal._boards = ()

    with pytest.raises(PortalError, match="No SmartRecruiters boards are configured"):
        run(portal.search(request_for("financial analyst")))


def test_a_rate_limit_stops_the_whole_portal() -> None:
    """A refusal is respected rather than routed around."""
    careers = FakeClient(error=PortalRateLimitedError("slow down", portal="smartrecruiters"))
    portal, _, _ = build_portal(["A", "B"], careers=careers)

    with pytest.raises(PortalRateLimitedError):
        run(portal.search(request_for("financial analyst")))


def test_a_block_stops_the_whole_portal() -> None:
    careers = FakeClient(error=PortalBlockedError("refused", portal="smartrecruiters"))
    portal, _, _ = build_portal(["A", "B"], careers=careers)

    with pytest.raises(PortalBlockedError):
        run(portal.search(request_for("financial analyst")))


def test_a_timeout_is_reported_as_a_portal_failure() -> None:
    careers = FakeClient(error=PortalTimeoutError("too slow", portal="smartrecruiters"))
    portal, _, _ = build_portal(careers=careers)

    with pytest.raises(PortalError):
        run(portal.search(request_for("financial analyst")))


def test_a_server_error_is_reported_as_a_portal_failure() -> None:
    careers = FakeClient(error=PortalError("HTTP 503", portal="smartrecruiters"))
    portal, _, _ = build_portal(careers=careers)

    with pytest.raises(PortalError):
        run(portal.search(request_for("financial analyst")))


def test_a_job_page_failing_drops_that_job_only() -> None:
    cards = [
        ("https://jobs.smartrecruiters.com/X/1000001-a", "Financial Analyst"),
        ("https://jobs.smartrecruiters.com/X/1000002-b", "Financial Analyst"),
    ]
    careers = FakeClient(default=board_page("X", cards))
    jobs = FakeClient({"https://jobs.smartrecruiters.com/X/1000001-a": job_page()})
    portal, _, _ = build_portal(["X"], careers=careers, jobs=jobs)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1


def test_a_malformed_job_page_drops_that_job_only() -> None:
    cards = [
        ("https://jobs.smartrecruiters.com/X/1000001-a", "Financial Analyst"),
        ("https://jobs.smartrecruiters.com/X/1000002-b", "Financial Analyst"),
    ]
    careers = FakeClient(default=board_page("X", cards))
    jobs = FakeClient(
        {
            "https://jobs.smartrecruiters.com/X/1000001-a": job_page(),
            "https://jobs.smartrecruiters.com/X/1000002-b": "<html>not a job</html>",
        }
    )
    portal, _, _ = build_portal(["X"], careers=careers, jobs=jobs)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1


def test_a_job_page_refusal_stops_the_portal() -> None:
    cards = [("https://jobs.smartrecruiters.com/X/1000001-a", "Financial Analyst")]
    careers = FakeClient(default=board_page("X", cards))
    jobs = FakeClient(error=PortalBlockedError("refused", portal="smartrecruiters"))
    portal, _, _ = build_portal(["X"], careers=careers, jobs=jobs)

    with pytest.raises(PortalBlockedError):
        run(portal.search(request_for("financial analyst")))


# --------------------------------------------------------------------------
# Result shape, contract, dedup
# --------------------------------------------------------------------------


def test_the_window_is_never_claimed_to_be_applied_by_the_portal() -> None:
    careers = FakeClient(default=board_page("X", []))
    portal, _, _ = build_portal(["X"], careers=careers)

    result = run(portal.search(request_for("analyst")))

    assert result.window_applied_by_portal is False
    assert result.has_more is False
    assert result.next_cursor is None
    assert result.window is FreshnessWindow.LAST_30D


def test_fetch_description_returns_what_search_already_collected() -> None:
    portal, _, _ = build_portal()
    job = portal._to_raw_job(JOB_US, url=JOB_US_URL)

    assert job is not None
    assert run(portal.fetch_description(job)) == job.description


def test_validate_url_probes_the_jobs_host() -> None:
    portal, _, jobs_client = build_portal()

    assert run(portal.validate_url(JOB_US_URL)) is True
    assert jobs_client.requests == [f"HEAD {JOB_US_URL}"]


def test_health_check_reports_rather_than_raising() -> None:
    careers = FakeClient(error=PortalError("down", portal="smartrecruiters"))
    portal, _, _ = build_portal(careers=careers)

    health = run(portal.health_check())

    assert health.is_available is False
    assert health.detail


def test_health_check_passes_when_a_board_answers() -> None:
    careers = FakeClient(default=board_page(BOARD, []))
    portal, _, _ = build_portal(careers=careers)

    assert run(portal.health_check()).is_available is True


def test_smartrecruiters_urls_already_classify_as_ats() -> None:
    """No change to url classification was needed; this pins that."""
    for url in (
        JOB_US_URL,
        f"{CAREERS_BASE}/{BOARD}",
        "https://jobs.smartrecruiters.com/external-referrals/company/X/publication/abc",
    ):
        assert classify_url(url) is JobUrlKind.ATS


def test_two_postings_from_one_employer_share_a_canonical_company() -> None:
    """The employer comes from the page, so grouping cannot fragment."""
    from app.processing.normalize import normalize_raw_job

    portal, _, _ = build_portal()
    first = portal._to_raw_job(JOB_US, url=JOB_US_URL)
    second_page = JOB_US.replace("744000147309143", "744000147309144")
    second = portal._to_raw_job(
        second_page, url=JOB_US_URL.replace("744000147309143", "744000147309144")
    )

    assert first is not None and second is not None
    assert first.company == second.company == "Wellmark, Inc."
    assert normalize_raw_job(first).canonical_key == normalize_raw_job(second).canonical_key


def test_clean_text_collapses_whitespace_and_strips_markup() -> None:
    assert clean_text("  <b>Senior</b>\n  Analyst  ") == "Senior Analyst"
    assert clean_text(None) is None
    assert clean_text("   ") is None
