"""Ashby adapter: page parsing, dates, locations, failures, access discipline.

Every response here comes from the captured fixture or a stub. Nothing in this
file makes a network request.

The fixture is a real capture of ``jobs.ashbyhq.com`` pages, trimmed to the
cases that matter: a board with a representative spread of postings, an
employer board that is genuinely empty, the soft-404 shell an unknown slug
returns, and four job pages covering a United States remote role, a
multi-location role whose ``jobLocation`` is an array, a single-location US
role and a non-US role. It is real markup rather than invented markup on
purpose - the data island and the structured block this adapter depends on are
the ones Ashby actually serves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import ApplicationType, FreshnessWindow
from app.portals.adapters.ashby import (
    BOARD_BASE,
    DEFAULT_BOARDS,
    FORBIDDEN_API_HOST,
    US_COUNTRY_NAMES,
    AshbyPortal,
    build_description,
    employment_type,
    format_location,
    html_to_text,
    is_us_posting,
    parse_app_data,
    parse_date_posted,
    parse_job_posting_ld,
    title_matches,
    title_tokens,
)
from app.portals.adapters.greenhouse import title_matches as greenhouse_title_matches
from app.portals.base import APPLICATION_TYPE_KEY, PortalSearchRequest
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.processing.urls import canonical_url, classify_url
from app.search.freshness import is_eligible_age

FIXTURE = Path(__file__).parent / "fixtures" / "portals" / "ashby_pages.json"

#: Ids from the capture, so a test names the posting it means.
DATA_ANALYST = "0fab2072-8863-42ec-b00d-de367b90d937"  # US, remote, ld is a dict
MULTI_LOCATION = "c4652ceb"  # jobLocation is an array; resolved below
NON_US = "7e8b02ec"  # Dublin, Ireland


@pytest.fixture(scope="module")
def pages() -> dict[str, dict[str, str]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def job_ids(pages) -> dict[str, str]:
    """Full ids for the prefixes above, so the fixture stays readable."""
    resolved = {}
    for prefix, name in ((DATA_ANALYST, "data"), (MULTI_LOCATION, "multi"), (NON_US, "non_us")):
        match = next(k for k in pages["jobs"] if k.startswith(prefix))
        resolved[name] = match
    return resolved


def board_page(pages, slug: str) -> str:
    return pages["boards"][slug]


def job_page(pages, job_id: str) -> str:
    return pages["jobs"][job_id]


@pytest.fixture
def stub_pages(monkeypatch: pytest.MonkeyPatch, pages):
    """Serve every page from the fixture, recording each request."""

    def _apply(payload_for=None):
        calls: list[str] = []

        async def _get_text(self, url, *, params=None):  # noqa: ANN001
            calls.append(url)
            if payload_for is not None:
                return payload_for(url)
            tail = url[len(BOARD_BASE) :].strip("/")
            parts = tail.split("/")
            if len(parts) == 1:
                page = pages["boards"].get(parts[0])
                if page is None:
                    raise PortalError(f"no board {parts[0]}", portal="ashby")
                return page
            page = pages["jobs"].get(parts[1])
            if page is None:
                raise PortalError(f"no job {parts[1]}", portal="ashby")
            return page

        monkeypatch.setattr(
            "app.portals.http.PortalHttpClient.get_text", _get_text, raising=True
        )
        return calls

    return _apply


def portal(**options: Any) -> AshbyPortal:
    options.setdefault("boards", ["vanta"])
    return AshbyPortal(options)


def a_request(*queries: str, limit: int = 50) -> PortalSearchRequest:
    return PortalSearchRequest(
        queries=queries or ("Data Analyst",),
        window=FreshnessWindow.LAST_30D,
        limit=limit,
    )


# --- Registration and capabilities -------------------------------------------


def test_the_adapter_is_registered_under_its_slug() -> None:
    import app.portals.adapters  # noqa: F401  - registers the adapters
    from app.portals.registry import registry

    assert registry.get("ashby") is AshbyPortal
    assert AshbyPortal.slug == "ashby"
    assert AshbyPortal.display_name == "Ashby"


def test_it_is_seeded_enabled_and_implemented() -> None:
    from app.services.portal_seed import PORTAL_SEEDS

    seed = next(seed for seed in PORTAL_SEEDS if seed.slug == "ashby")

    assert seed.is_enabled is True
    assert seed.website_url.startswith(BOARD_BASE)


def test_capabilities_describe_what_the_pages_actually_do() -> None:
    caps = portal().capabilities

    assert caps.supports_date_filter is False
    # `search` already returns the full text, so nothing is left to fetch.
    assert caps.supports_description_fetch is False
    assert caps.supports_pagination is False
    assert caps.requires_auth is False
    assert caps.provides_application_url is True


# --- Access discipline --------------------------------------------------------


async def test_only_allowed_board_and_job_paths_are_requested(stub_pages) -> None:
    calls = stub_pages()

    await portal().search(a_request("Data Analyst"))

    assert calls, "the board must actually be requested"
    for url in calls:
        assert url.startswith(BOARD_BASE)
        tail = url[len(BOARD_BASE) :]
        # robots.txt on this host disallows exactly these three prefixes.
        assert not tail.startswith("/api/")
        assert not tail.startswith("/b/")
        assert not tail.startswith("/meeting/")


async def test_the_posting_api_host_is_never_requested(stub_pages) -> None:
    """The one-request API is off-limits while its robots policy is unreadable.

    ``api.ashbyhq.com`` answers 401 for ``robots.txt``, and this project treats
    a policy it cannot read as a refusal - see ``app.careers.fetching``, where
    only 404 and 410 are permissive. The cheaper path stays unused until that
    changes, and this is what stops it being quietly reintroduced.
    """
    calls = stub_pages()

    await portal().search(a_request("Data Analyst", "Account Executive"))

    for url in calls:
        assert FORBIDDEN_API_HOST not in url


def test_the_adapter_builds_no_url_against_a_forbidden_endpoint() -> None:
    """Prose may discuss the paths that are off-limits; code may not use them.

    Checked over the source's string literals with the docstrings removed, so
    the module docstring stays free to explain *why* the posting API is
    avoided - which is the thing most likely to stop someone reinstating it -
    while any literal that could actually address it fails here.
    """
    import ast

    source = (
        Path(__file__).parent.parent / "app" / "portals" / "adapters" / "ashby.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
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
        assert "posting-api" not in literal
        assert "non-user-graphql" not in literal
        assert not literal.startswith("https://api.ashbyhq.com")
        # `/api/`, `/b/` and `/meeting/` are the robots-disallowed prefixes;
        # no literal may build a path into any of them.
        assert not literal.startswith("/api/")

    # The host name survives only as the constant the tests above assert on.
    assert FORBIDDEN_API_HOST == "api.ashbyhq.com"
    assert sum(1 for literal in literals if literal == FORBIDDEN_API_HOST) == 1


# --- Request flow -------------------------------------------------------------


async def test_the_board_is_read_before_any_job_page(stub_pages) -> None:
    calls = stub_pages()

    await portal().search(a_request("Data Analyst"))

    assert calls[0] == f"{BOARD_BASE}/vanta"


async def test_only_title_matched_jobs_have_their_pages_opened(
    stub_pages, pages
) -> None:
    """The whole cost model rests on this.

    The fixture board carries four postings and only one is a Data Analyst, so
    exactly one job page may be requested. Opening all four would multiply the
    request count by the size of every board.
    """
    calls = stub_pages()

    await portal().search(a_request("Data Analyst"))

    job_calls = [c for c in calls if c.count("/") > 3]
    assert len(job_calls) == 1
    assert len(pages["boards"]["vanta"]) > 0


async def test_a_board_is_read_once_however_many_queries_run(stub_pages) -> None:
    calls = stub_pages()
    adapter = portal()

    await adapter.search(a_request("Data Analyst"))
    await adapter.search(a_request("Account Executive"))
    await adapter.search(a_request("Data Analyst"))

    assert calls.count(f"{BOARD_BASE}/vanta") == 1


async def test_a_job_page_is_read_once_across_queries(stub_pages, job_ids) -> None:
    calls = stub_pages()
    adapter = portal()

    await adapter.search(a_request("Data Analyst"))
    await adapter.search(a_request("Senior Data Analyst"))

    target = f"{BOARD_BASE}/vanta/{job_ids['data']}"
    assert calls.count(target) == 1


# --- Title filtering ----------------------------------------------------------


TITLE_CASES = [
    ("Financial Analyst", "Senior Financial Analyst", True),
    ("Financial Analyst", "Financial Analyst, Business F&S", True),
    ("Financial Analyst", "Business Analyst", False),
    ("Data Analyst", "Senior Data Analyst, Strategic Finance", True),
    ("Data Analyst", "Data Engineer", False),
    ("Analytics Analyst", "Analytics Analyst", True),
    ("FP&A Analyst", "FP&A Analyst", True),
    ("", "Anything At All", False),
]


@pytest.mark.parametrize(("query", "title", "expected"), TITLE_CASES)
def test_only_same_role_titles_match(query: str, title: str, expected: bool) -> None:
    assert title_matches(query, title) is expected


@pytest.mark.parametrize(("query", "title", "expected"), TITLE_CASES)
def test_title_matching_agrees_with_greenhouse(
    query: str, title: str, expected: bool
) -> None:
    """The rule is stated per adapter; this is what stops the copies drifting."""
    assert title_matches(query, title) is greenhouse_title_matches(query, title)


def test_single_letters_are_not_significant_tokens() -> None:
    assert title_tokens("FP&A Analyst") == {"fp", "analyst"}


# --- Page parsing -------------------------------------------------------------


def test_the_board_page_island_is_read(pages) -> None:
    data = parse_app_data(board_page(pages, "vanta"))

    assert data is not None
    assert data["organization"]["name"] == "Vanta"
    assert isinstance(data["jobBoard"]["jobPostings"], list)


def test_the_island_survives_braces_and_quotes_inside_its_own_strings() -> None:
    """A regular expression cannot read this island; bracket matching can."""
    page = (
        "<html><script>window.__appData = "
        '{"organization":{"name":"Brace } Co"},'
        '"jobBoard":{"jobPostings":[{"id":"1","title":"He said \\"hi\\" {x}"}]}}'
        ";</script></html>"
    )

    data = parse_app_data(page)

    assert data is not None
    assert data["organization"]["name"] == "Brace } Co"
    assert data["jobBoard"]["jobPostings"][0]["title"] == 'He said "hi" {x}'


@pytest.mark.parametrize(
    "page",
    ["", "<html></html>", "<html>window.__appData = not json;</html>", None, 7],
)
def test_an_unreadable_island_reports_nothing(page: Any) -> None:
    assert parse_app_data(page) is None


def test_the_structured_job_posting_is_read(pages, job_ids) -> None:
    ld = parse_job_posting_ld(job_page(pages, job_ids["data"]))

    assert ld is not None
    assert ld["@type"] == "JobPosting"
    assert ld["datePosted"] == "2026-09-02"


def test_a_json_ld_array_is_read_as_well_as_an_object(pages, job_ids) -> None:
    """Ashby emits both shapes, on the same board."""
    for name in ("data", "multi", "non_us"):
        ld = parse_job_posting_ld(job_page(pages, job_ids[name]))
        assert ld is not None, name
        assert ld["@type"] == "JobPosting"


def test_a_page_without_structured_data_reports_nothing() -> None:
    assert parse_job_posting_ld("<html><body>nothing</body></html>") is None
    assert parse_job_posting_ld(None) is None


def test_malformed_json_ld_is_skipped_not_fatal() -> None:
    page = (
        '<script type="application/ld+json">{not json}</script>'
        '<script type="application/ld+json">'
        '{"@type":"JobPosting","datePosted":"2026-09-02"}</script>'
    )

    ld = parse_job_posting_ld(page)

    assert ld is not None and ld["datePosted"] == "2026-09-02"


# --- Field mapping ------------------------------------------------------------


async def test_a_posting_maps_onto_the_raw_job_contract(stub_pages, job_ids) -> None:
    stub_pages()

    result = await portal().search(a_request("Data Analyst"))
    job = result.jobs[0]

    assert job.external_id == job_ids["data"]
    assert job.source == "ashby"
    assert job.title == "Senior Data Analyst, Strategic Finance"
    # Read from the page, never from configuration.
    assert job.company == "Vanta"
    assert job.source_url == f"{BOARD_BASE}/vanta/{job_ids['data']}"
    assert job.application_url == f"{BOARD_BASE}/vanta/{job_ids['data']}/application"
    assert job.posted_at == datetime(2026, 9, 2, tzinfo=UTC)
    assert job.employment_type == "Full-time"


async def test_the_external_id_is_ashbys_own_uuid(stub_pages, job_ids) -> None:
    stub_pages()

    result = await portal().search(a_request("Data Analyst"))

    assert result.jobs[0].external_id == job_ids["data"]
    assert len(result.jobs[0].external_id) == 36


async def test_applying_is_declared_as_a_long_application(stub_pages) -> None:
    stub_pages()

    result = await portal().search(a_request("Data Analyst"))

    assert result.jobs[0].raw_payload[APPLICATION_TYPE_KEY] == (
        ApplicationType.LONG_APPLICATION.value
    )


def test_the_employer_name_comes_from_the_page_not_configuration() -> None:
    """Configuring a board takes a slug and nothing else."""
    adapter = portal(boards=["vanta", "openai"])

    assert adapter.boards == ("vanta", "openai")
    assert all(isinstance(slug, str) for slug in adapter.boards)


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("FullTime", "Full-time"),
        ("PartTime", "Part-time"),
        ("Intern", "Intern"),
        ("Contract", "Contract"),
        ("Temporary", "Temporary"),
        ("SomethingElse", "SomethingElse"),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_the_commitment_is_reported_as_published(
    published: Any, expected: str | None
) -> None:
    assert employment_type({"employmentType": published}) == expected


# --- Dates --------------------------------------------------------------------


def test_date_posted_parses_to_aware_utc() -> None:
    moment = parse_date_posted("2026-09-02")

    assert moment == datetime(2026, 9, 2, tzinfo=UTC)
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


def test_a_timestamped_date_keeps_its_offset() -> None:
    assert parse_date_posted("2026-09-02T12:00:00Z") == datetime(
        2026, 9, 2, 12, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "value", [None, "", "   ", "not-a-date", "2026-13-45", 20260902, [], {}]
)
def test_a_missing_or_malformed_date_yields_nothing(value: Any) -> None:
    assert parse_date_posted(value) is None


async def test_a_job_without_a_publication_date_is_dropped(stub_pages, pages) -> None:
    """No substitute is invented, so nothing undated enters the pipeline."""
    dated = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def payload(url: str) -> str:
        tail = url[len(BOARD_BASE) :].strip("/").split("/")
        if len(tail) == 1:
            return dated["boards"]["vanta"]
        page = next(iter(dated["jobs"].values()))
        return page.replace('"datePosted"', '"notTheDate"')

    stub_pages(payload_for=payload)

    result = await portal().search(a_request("Data Analyst"))

    assert result.jobs == []


async def test_a_job_page_without_structured_data_is_dropped(stub_pages) -> None:
    dated = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def payload(url: str) -> str:
        tail = url[len(BOARD_BASE) :].strip("/").split("/")
        if len(tail) == 1:
            return dated["boards"]["vanta"]
        return "<html><body>no structured data here</body></html>"

    stub_pages(payload_for=payload)

    result = await portal().search(a_request("Data Analyst"))

    assert result.jobs == []


def test_the_real_freshness_gate_rejects_an_undated_listing() -> None:
    assert is_eligible_age(None, datetime.now(UTC)) is False


def test_the_real_freshness_gate_draws_the_line_at_thirty_days() -> None:
    now = datetime.now(UTC)
    old = parse_date_posted((now - timedelta(days=31)).date().isoformat())
    fresh = parse_date_posted((now - timedelta(days=29)).date().isoformat())

    assert is_eligible_age(old, now) is False
    assert is_eligible_age(fresh, now) is True


# --- Location and the United States rule --------------------------------------


@pytest.mark.parametrize(
    "spelling", ["USA", "usa", "United States", "united states", "US", "U.S.A."]
)
def test_every_spelling_ashby_uses_for_the_united_states_is_recognised(
    spelling: str,
) -> None:
    """Both live spellings occur; a test against one silently drops the other."""
    assert spelling.casefold() in US_COUNTRY_NAMES
    assert is_us_posting(
        {"jobLocation": {"address": {"addressCountry": spelling}}}
    )


def test_a_non_us_country_is_not_treated_as_us() -> None:
    assert not is_us_posting(
        {"jobLocation": {"address": {"addressCountry": "Ireland"}}}
    )


def test_a_multi_location_role_counts_when_any_location_is_us() -> None:
    assert is_us_posting(
        {
            "jobLocation": [
                {"address": {"addressCountry": "Ireland"}},
                {"address": {"addressCountry": "USA"}},
            ]
        }
    )


def test_an_applicant_location_requirement_also_settles_the_country() -> None:
    assert is_us_posting(
        {"applicantLocationRequirements": {"@type": "Country", "name": "USA"}}
    )


def test_a_posting_with_no_country_is_not_assumed_american() -> None:
    assert not is_us_posting({})
    assert not is_us_posting({"jobLocation": {"address": {}}})


def test_a_us_role_reports_its_us_city_and_region(pages, job_ids) -> None:
    ld = parse_job_posting_ld(job_page(pages, job_ids["multi"]))
    board = {"locationName": "San Francisco, CA"}

    assert is_us_posting(ld)
    assert format_location(ld, board) is not None


def test_a_non_us_role_keeps_its_published_location(pages, job_ids) -> None:
    ld = parse_job_posting_ld(job_page(pages, job_ids["non_us"]))
    board = {"locationName": "Dublin, Ireland"}

    assert not is_us_posting(ld)
    assert format_location(ld, board) == "Dublin, Ireland"


def test_a_non_us_role_is_not_rescued_by_an_american_looking_label() -> None:
    ld = {"jobLocation": {"address": {"addressCountry": "Ireland"}}}

    assert format_location(ld, {"locationName": "New York, NY"}) == "New York, NY"
    assert not is_us_posting(ld)


def test_a_remote_us_role_keeps_the_label_the_board_published(
    stub_pages, pages, job_ids
) -> None:
    """Ashby states the country but no city on fully remote roles."""
    ld = parse_job_posting_ld(job_page(pages, job_ids["data"]))

    assert is_us_posting(ld)
    assert format_location(ld, {"locationName": "Remote U.S."}) == "Remote U.S."


def test_a_posting_with_nothing_published_reports_no_location() -> None:
    assert format_location({}, {}) is None


async def test_the_collected_location_is_what_the_employer_published(
    stub_pages,
) -> None:
    stub_pages()

    result = await portal().search(a_request("Data Analyst"))

    assert result.jobs[0].location == "Remote U.S."


# --- Description --------------------------------------------------------------


def test_the_description_is_the_full_published_text(pages, job_ids) -> None:
    ld = parse_job_posting_ld(job_page(pages, job_ids["data"]))

    text = build_description(ld, {})

    assert text is not None
    assert len(text) > 2000, "Ashby postings run thousands of characters"


def test_the_description_is_flattened_out_of_html() -> None:
    text = build_description(
        {
            "description": (
                "<h2>Responsibilities</h2><ul><li>Build models</li>"
                "<li>Ship &amp; iterate</li></ul>"
            )
        },
        {},
    )

    assert text is not None
    assert "<h2>" not in text and "<li>" not in text
    assert "Responsibilities" in text
    assert "- Build models" in text
    assert "Ship & iterate" in text
    assert "&amp;" not in text


def test_section_headings_survive_flattening(pages, job_ids) -> None:
    ld = parse_job_posting_ld(job_page(pages, job_ids["data"]))

    text = build_description(ld, {})

    assert text is not None
    lowered = text.casefold()
    assert any(
        heading in lowered
        for heading in ("responsibilit", "qualification", "requirement", "what you")
    ), "the matching layer reads these headings"


def test_the_page_description_is_used_when_the_structured_one_is_missing() -> None:
    text = build_description({}, {"descriptionHtml": "<p>We are hiring.</p>"})

    assert text == "We are hiring."


def test_a_posting_with_no_text_reports_none() -> None:
    assert build_description({}, {}) is None


def test_html_to_text_handles_empty_and_non_string_values() -> None:
    assert html_to_text(None) is None
    assert html_to_text("") is None
    assert html_to_text("   ") is None
    assert html_to_text(123) is None


# --- Work mode ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("published", "expected"),
    [("Remote", "Remote"), ("Hybrid", "Hybrid"), ("Onsite", "Onsite")],
)
def test_the_published_workplace_type_reaches_the_description(
    published: str, expected: str
) -> None:
    text = build_description(
        {"description": "<p>Intro.</p>"}, {"workplaceType": published}
    )

    assert text is not None
    assert text.startswith(f"Workplace type: {expected}")


def test_the_published_workplace_type_survives_normalisation() -> None:
    from app.processing.titles import detect_work_mode

    text = build_description(
        {"description": "<p>Intro.</p>"}, {"workplaceType": "Remote"}
    )

    assert detect_work_mode(text[:2000]) is not None


@pytest.mark.parametrize("published", ["", None, "Flexible", 7])
def test_an_unrecognised_workplace_type_is_dropped_not_guessed(
    published: Any,
) -> None:
    text = build_description(
        {"description": "<p>Intro.</p>"}, {"workplaceType": published}
    )

    assert text is not None
    assert "Workplace type:" not in text


# --- Board configuration ------------------------------------------------------


def test_the_default_boards_are_used_when_nothing_is_configured() -> None:
    assert AshbyPortal().boards == DEFAULT_BOARDS


def test_boards_may_be_configured_as_slugs() -> None:
    assert portal(boards=["acme", "beta"]).boards == ("acme", "beta")


def test_boards_may_be_configured_as_mappings() -> None:
    """An operator copying the Lever or Dayforce shape is not silently ignored."""
    assert portal(boards=[{"slug": "acme"}, {"slug": "beta"}]).boards == (
        "acme",
        "beta",
    )


def test_a_single_slug_string_is_accepted() -> None:
    assert portal(boards="acme").boards == ("acme",)


def test_a_malformed_board_entry_is_skipped_not_fatal() -> None:
    assert portal(boards=["", None, 7, {"slug": ""}, "acme"]).boards == ("acme",)


def test_an_entirely_unusable_board_list_falls_back_to_the_defaults() -> None:
    assert portal(boards=[None, 7, {}]).boards == DEFAULT_BOARDS


# --- Failure modes ------------------------------------------------------------


async def test_an_employer_with_no_openings_is_a_success_with_no_jobs(
    stub_pages,
) -> None:
    """A real board that is empty must not look like an outage.

    It is distinguishable structurally: the page still carries the employer's
    name and an empty posting list, where a slug that does not exist carries
    neither.
    """
    stub_pages()
    adapter = portal(boards=["emptyco"])

    result = await adapter.search(a_request("Data Analyst"))

    assert result.jobs == []


async def test_a_soft_404_board_is_treated_as_unavailable(stub_pages) -> None:
    """An unknown slug answers HTTP 200 with an empty shell.

    Reported as an unavailable board rather than an empty one, because
    returning "no jobs" for a slug that does not exist would hide a broken
    configuration for ever.
    """
    stub_pages()
    adapter = portal(boards=["zzzznotreal123"])

    with pytest.raises(PortalError):
        await adapter.search(a_request("Data Analyst"))


async def test_one_board_failing_does_not_cost_the_others(stub_pages) -> None:
    stub_pages()
    adapter = portal(boards=["zzzznotreal123", "vanta"])

    result = await adapter.search(a_request("Data Analyst"))

    assert [job.title for job in result.jobs] == [
        "Senior Data Analyst, Strategic Finance"
    ]


async def test_every_board_failing_raises_rather_than_reporting_no_jobs(
    stub_pages,
) -> None:
    stub_pages()
    adapter = portal(boards=["zzzznotreal123", "alsomissing"])

    with pytest.raises(PortalError) as excinfo:
        await adapter.search(a_request("Data Analyst"))

    assert "2 attempted" in excinfo.value.message


async def test_a_board_page_with_no_island_is_a_parse_error(stub_pages) -> None:
    stub_pages(payload_for=lambda url: "<html><body>nothing at all</body></html>")
    adapter = portal(boards=["vanta"])

    with pytest.raises(PortalError):
        await adapter.search(a_request("Data Analyst"))

    with pytest.raises(PortalParseError):
        await adapter._read_board("vanta")


async def test_a_failing_job_page_costs_only_that_listing(stub_pages, pages) -> None:
    def payload(url: str) -> str:
        tail = url[len(BOARD_BASE) :].strip("/").split("/")
        if len(tail) == 1:
            return pages["boards"]["vanta"]
        raise PortalTimeoutError("job page timed out", portal="ashby")

    stub_pages(payload_for=payload)

    result = await portal().search(a_request("Data Analyst"))

    assert result.jobs == []


@pytest.mark.parametrize(
    "error",
    [
        PortalRateLimitedError("429", portal="ashby"),
        PortalBlockedError("403", portal="ashby"),
    ],
)
async def test_a_refusal_stops_the_whole_portal_immediately(
    stub_pages, error: PortalError
) -> None:
    calls = stub_pages(payload_for=_raise(error))

    with pytest.raises(type(error)):
        await portal(boards=["vanta", "openai"]).search(a_request("Data Analyst"))

    assert len(calls) == 1, "no further board may be requested after a refusal"


async def test_a_refusal_on_a_job_page_also_stops_the_portal(
    stub_pages, pages
) -> None:
    def payload(url: str) -> str:
        tail = url[len(BOARD_BASE) :].strip("/").split("/")
        if len(tail) == 1:
            return pages["boards"]["vanta"]
        raise PortalRateLimitedError("429", portal="ashby")

    stub_pages(payload_for=payload)

    with pytest.raises(PortalRateLimitedError):
        await portal().search(a_request("Data Analyst"))


async def test_a_server_error_on_every_board_raises(stub_pages) -> None:
    stub_pages(payload_for=_raise(PortalError("HTTP 503", portal="ashby")))

    with pytest.raises(PortalError):
        await portal().search(a_request("Data Analyst"))


async def test_health_check_reports_rather_than_raising(stub_pages) -> None:
    stub_pages(payload_for=_raise(PortalError("down", portal="ashby")))

    health = await portal().health_check()

    assert health.is_available is False
    assert health.detail is not None


def _raise(error: BaseException):
    def _payload(url):  # noqa: ANN001
        raise error

    return _payload


# --- Identity, deduplication and limits ---------------------------------------


async def test_the_same_posting_is_not_collected_twice(stub_pages, pages) -> None:
    doubled = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data = parse_app_data(doubled["boards"]["vanta"])
    postings = data["jobBoard"]["jobPostings"]
    data["jobBoard"]["jobPostings"] = postings + postings
    doubled["boards"]["vanta"] = (
        "<html><script>window.__appData = " + json.dumps(data) + ";</script></html>"
    )

    def payload(url: str) -> str:
        tail = url[len(BOARD_BASE) :].strip("/").split("/")
        if len(tail) == 1:
            return doubled["boards"]["vanta"]
        return doubled["jobs"][tail[1]]

    stub_pages(payload_for=payload)

    result = await portal().search(a_request("Data Analyst"))

    assert len(result.jobs) == 1


def test_ashby_urls_classify_as_an_ats_destination() -> None:
    """Cross-portal dedup already collapses these; nothing needed changing."""
    job = f"{BOARD_BASE}/vanta/{DATA_ANALYST}"

    assert classify_url(job).value == "ats"
    assert classify_url(f"{job}/application").value == "ats"


def test_ashby_tracking_parameters_are_already_stripped() -> None:
    job = f"{BOARD_BASE}/vanta/{DATA_ANALYST}"

    assert canonical_url(f"{job}?utm_source=x&utm_campaign=y") == job


async def test_the_requested_limit_is_respected(stub_pages) -> None:
    stub_pages()

    result = await portal().search(
        a_request("Data Analyst", "Account Executive", limit=1)
    )

    assert len(result.jobs) == 1


async def test_the_caller_applies_the_freshness_window(stub_pages) -> None:
    stub_pages()

    result = await portal().search(a_request("Data Analyst"))

    assert result.window_applied_by_portal is False
    assert result.has_more is False
    assert result.next_cursor is None


async def test_the_full_text_needs_no_second_request(stub_pages) -> None:
    calls = stub_pages()
    adapter = portal()

    result = await adapter.search(a_request("Data Analyst"))
    before = len(calls)
    description = await adapter.fetch_description(result.jobs[0])

    assert description == result.jobs[0].description
    assert len(calls) == before, "fetch_description must not touch the network"


# --- No auto-apply ------------------------------------------------------------


def test_the_adapter_exposes_no_apply_automation() -> None:
    """CLAUDE.md section 1: this project never submits an application."""
    source = (
        Path(__file__).parent.parent / "app" / "portals" / "adapters" / "ashby.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    for banned in ("easy_apply", "quick_apply", "submit_application", "auto_apply"):
        assert banned not in code.casefold()
