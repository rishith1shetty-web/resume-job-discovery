"""Lever adapter: parsing, board configuration, title matching, failures.

Every response here comes from the captured fixture or a stub. Nothing in this
file makes a network request.

The fixture is a real capture of ``/v0/postings/<board>?mode=json`` for two
live boards, trimmed to the postings that exercise the cases that matter: a
United-States multi-location role, a non-US multi-location role, remote, hybrid
and onsite roles, several commitment vocabularies, and a posting with the full
five-section structure Lever publishes. It is real data rather than invented
data on purpose - the field shapes this adapter depends on are the ones the
employer actually serves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import ApplicationType, FreshnessWindow
from app.portals.adapters.greenhouse import title_matches as greenhouse_title_matches
from app.portals.adapters.lever import (
    API_BASE,
    DEFAULT_BOARDS,
    LeverBoard,
    LeverPortal,
    build_description,
    format_location,
    html_to_text,
    parse_created_at,
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
from app.processing.urls import canonical_url, classify_url
from app.search.freshness import is_eligible_age

FIXTURE = Path(__file__).parent / "fixtures" / "portals" / "lever_postings.json"

#: Ids from the capture, so a test names the posting it means.
US_MULTI = "600a4ac7-6ffa-4ced-8d34-253d00147aa9"  # Spotify, US, New York + Stockholm
NON_US_MULTI = "2193db3f-77c5-43b8-b030-8f92c9882bf1"  # Spotify, GB, London + Stockholm
ANALYTICS_ENGINEER = "bfa7bc15-793e-486c-a3f5-b27d84c53025"  # Gopuff, 5 sections


@pytest.fixture(scope="module")
def captured() -> dict[str, list[dict[str, Any]]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def record_by_id(
    captured: dict[str, list[dict[str, Any]]], job_id: str
) -> dict[str, Any]:
    for postings in captured.values():
        for record in postings:
            if record.get("id") == job_id:
                return record
    raise AssertionError(f"fixture has no posting {job_id}")


@pytest.fixture
def stub_boards(monkeypatch: pytest.MonkeyPatch, captured):
    """Serve each board from the fixture, recording every request."""

    def _apply(payload_for=None, boards=captured):
        calls: list[dict[str, Any]] = []

        async def _get_text(self, url, *, params=None):  # noqa: ANN001
            calls.append({"url": url, "params": params or {}})
            if payload_for is not None:
                return payload_for(url, params or {})
            slug = url.rstrip("/").split("/")[-1]
            postings = boards.get(slug)
            if postings is None:
                raise PortalError(f"board {slug} not found", portal="lever")
            return json.dumps(postings)

        monkeypatch.setattr(
            "app.portals.http.PortalHttpClient.get_text", _get_text, raising=True
        )
        return calls

    return _apply


def portal(**options: Any) -> LeverPortal:
    """An adapter reading the fixture's boards unless told otherwise."""
    options.setdefault(
        "boards",
        [
            {"slug": "spotify", "company": "Spotify"},
            {"slug": "gopuff", "company": "Gopuff"},
        ],
    )
    return LeverPortal(options)


def a_request(*queries: str, limit: int = 50) -> PortalSearchRequest:
    return PortalSearchRequest(
        queries=queries or ("Analytics Engineer",),
        window=FreshnessWindow.LAST_30D,
        limit=limit,
    )


# --- Registration and capabilities -------------------------------------------


def test_the_adapter_is_registered_under_its_slug() -> None:
    import app.portals.adapters  # noqa: F401  - registers the adapters
    from app.portals.registry import registry

    assert registry.get("lever") is LeverPortal
    assert LeverPortal.slug == "lever"
    assert LeverPortal.display_name == "Lever"


def test_capabilities_describe_what_the_feed_actually_does() -> None:
    caps = portal().capabilities

    # No date filter and no second fetch: freshness is applied by the caller,
    # and the full posting text arrives with the listing.
    assert caps.supports_date_filter is False
    assert caps.supports_description_fetch is False
    # `limit`/`skip` exist but one request returns the board in full, so this
    # adapter never pages and must not claim it does.
    assert caps.supports_pagination is False
    assert caps.requires_auth is False
    assert caps.provides_application_url is True


# --- Request construction -----------------------------------------------------


async def test_one_request_per_board_against_the_public_feed(stub_boards) -> None:
    calls = stub_boards()

    await portal().search(a_request("Analytics Engineer"))

    assert [call["url"] for call in calls] == [
        f"{API_BASE}/spotify",
        f"{API_BASE}/gopuff",
    ]


async def test_only_the_mode_parameter_is_sent(stub_boards) -> None:
    """The feed's filter parameters are ignored by Lever and are not sent.

    ``?query=``, ``?search=`` and ``?workplaceType=`` all return the whole
    board unfiltered - measured on a 781-posting board, every one of them
    returned all 781 records. Sending them would look like filtering and would
    not be, so the adapter must send none of them.
    """
    calls = stub_boards()

    await portal().search(a_request("Financial Analyst", "FP&A Analyst"))

    for call in calls:
        assert call["params"] == {"mode": "json"}
        forbidden = {"query", "search", "workplaceType", "limit", "skip", "location"}
        assert not forbidden & set(call["params"])


async def test_a_board_is_fetched_once_however_many_queries_run(
    stub_boards,
) -> None:
    """Same-role variations must not multiply the request count."""
    calls = stub_boards()
    adapter = portal()

    await adapter.search(a_request("Analytics Engineer"))
    await adapter.search(a_request("Data Scientist"))
    await adapter.search(a_request("Android Engineer"))

    assert len(calls) == 2, "two boards, fetched once each across three searches"


# --- Title filtering ----------------------------------------------------------


#: Cases that decide whether a same-role filter is doing its job.
TITLE_CASES = [
    ("Financial Analyst", "Senior Financial Analyst", True),
    ("Financial Analyst", "Financial Analyst, Business F&S", True),
    ("Financial Analyst", "Business Analyst", False),
    ("Data Scientist", "Data Scientist - Music Mission", True),
    ("Analytics Engineer", "Analytics Engineer", True),
    ("Analytics Engineer", "Analytics Manager", False),
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
    """The rule is stated per adapter; this is what stops the copies drifting.

    No adapter in this package imports another - Greenhouse, Dayforce and
    Google Careers each state this rule for themselves - so Lever states it too
    rather than introducing the first cross-adapter dependency. Pinning the two
    answers together keeps the benefit of sharing without the coupling, and
    fails loudly if either side is ever changed alone.
    """
    assert title_matches(query, title) is greenhouse_title_matches(query, title)


def test_single_letters_are_not_significant_tokens() -> None:
    assert title_tokens("FP&A Analyst") == {"fp", "analyst"}


async def test_a_board_is_filtered_to_the_requested_role(stub_boards) -> None:
    stub_boards()

    result = await portal().search(a_request("Analytics Engineer"))

    assert [job.title for job in result.jobs] == ["Analytics Engineer"]


# --- Field mapping ------------------------------------------------------------


async def test_a_posting_maps_onto_the_raw_job_contract(stub_boards) -> None:
    stub_boards()

    result = await portal().search(a_request("Analytics Engineer"))
    job = result.jobs[0]

    assert job.external_id == ANALYTICS_ENGINEER
    assert job.source == "lever"
    assert job.title == "Analytics Engineer"
    # The feed carries no employer name anywhere; this comes from the board.
    assert job.company == "Gopuff"
    assert job.source_url == f"https://jobs.lever.co/gopuff/{ANALYTICS_ENGINEER}"
    assert job.application_url == (
        f"https://jobs.lever.co/gopuff/{ANALYTICS_ENGINEER}/apply"
    )
    assert job.employment_type == "Full Time"
    assert job.posted_at == datetime(2026, 9, 10, 15, 4, 44, 716000, tzinfo=UTC)


async def test_the_external_id_is_levers_own_uuid(stub_boards) -> None:
    stub_boards()

    result = await portal().search(a_request("Analytics Engineer"))

    assert result.jobs[0].external_id == ANALYTICS_ENGINEER
    assert len(result.jobs[0].external_id) == 36


async def test_applying_is_declared_as_a_long_application(stub_boards) -> None:
    """Lever is the employer's application system, not a one-click board."""
    stub_boards()

    result = await portal().search(a_request("Analytics Engineer"))

    assert result.jobs[0].raw_payload[APPLICATION_TYPE_KEY] == (
        ApplicationType.LONG_APPLICATION.value
    )


def test_a_posting_without_an_id_title_or_link_is_skipped() -> None:
    adapter = portal()
    board = LeverBoard("gopuff", "Gopuff")

    for missing in ("id", "text", "hostedUrl"):
        record = {
            "id": "x",
            "text": "Analytics Engineer",
            "hostedUrl": "https://jobs.lever.co/gopuff/x",
        }
        record.pop(missing)
        assert adapter._to_raw_job(record, board=board) is None


def test_a_posting_with_no_apply_url_keeps_none_rather_than_a_guess() -> None:
    job = portal()._to_raw_job(
        {
            "id": "x",
            "text": "Analytics Engineer",
            "hostedUrl": "https://jobs.lever.co/gopuff/x",
        },
        board=LeverBoard("gopuff", "Gopuff"),
    )

    assert job is not None
    assert job.application_url is None


def test_a_board_configured_without_a_company_leaves_it_unknown() -> None:
    """Honest absence beats a name derived from the slug."""
    job = portal()._to_raw_job(
        {
            "id": "x",
            "text": "Analytics Engineer",
            "hostedUrl": "https://jobs.lever.co/somewhere/x",
        },
        board=LeverBoard("somewhere", None),
    )

    assert job is not None
    assert job.company is None


# --- Dates --------------------------------------------------------------------


def test_created_at_converts_from_epoch_milliseconds() -> None:
    assert parse_created_at(1789052684716) == datetime(
        2026, 9, 10, 15, 4, 44, 716000, tzinfo=UTC
    )


def test_a_converted_date_is_timezone_aware_utc() -> None:
    moment = parse_created_at(1789052684716)

    assert moment is not None
    assert moment.tzinfo is not None
    assert moment.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "value",
    [None, "", "1789052684716", "not-a-date", [], {}, True, float("nan")],
)
def test_a_missing_or_malformed_created_at_yields_no_date(value: Any) -> None:
    """No substitute is invented, so the freshness gate rejects the listing.

    CLAUDE.md section 6: a date that cannot be verified must never be treated
    as recent. ``createdAt`` is the only timestamp in the feed, so there is
    nothing to fall back to - and falling back to the fetch time would make
    every undated posting look like it was published today.
    """
    result = parse_created_at(value)

    assert result is None or result != result  # NaN is its own failure
    if value is not float("nan"):
        assert parse_created_at(value) is None or isinstance(result, datetime)


def test_a_missing_created_at_leaves_posted_at_empty() -> None:
    job = portal()._to_raw_job(
        {
            "id": "x",
            "text": "Analytics Engineer",
            "hostedUrl": "https://jobs.lever.co/gopuff/x",
        },
        board=LeverBoard("gopuff", "Gopuff"),
    )

    assert job is not None
    assert job.posted_at is None


@pytest.mark.parametrize("value", [10**15, -(10**15)])
def test_an_out_of_range_timestamp_is_refused_rather_than_raising(
    value: int,
) -> None:
    assert parse_created_at(value) is None


def test_an_undated_posting_fails_the_real_freshness_gate() -> None:
    """Wired to production's own gate, not to a restatement of it."""
    assert is_eligible_age(None, datetime.now(UTC)) is False


def test_an_over_age_posting_fails_the_real_freshness_gate() -> None:
    now = datetime.now(UTC)
    old = parse_created_at(int((now - timedelta(days=31)).timestamp() * 1000))
    fresh = parse_created_at(int((now - timedelta(days=29)).timestamp() * 1000))

    assert is_eligible_age(old, now) is False
    assert is_eligible_age(fresh, now) is True


# --- Location and the United States rule --------------------------------------


def test_a_us_coded_posting_prefers_its_us_location(captured) -> None:
    """New York over Stockholm: the US entry is the one that matters."""
    record = record_by_id(captured, US_MULTI)

    assert record["country"] == "US"
    assert record["categories"]["allLocations"] == ["New York, NY", "Stockholm"]
    assert format_location(record) == "New York, NY"


def test_a_non_us_posting_keeps_its_published_location(captured) -> None:
    """A GB posting is not relabelled because another office reads as American.

    The country code gates the preference. Inferring a country the employer did
    not claim is exactly what CLAUDE.md section 6 and the US rule forbid.
    """
    record = record_by_id(captured, NON_US_MULTI)

    assert record["country"] == "GB"
    assert format_location(record) == "London"


def test_a_non_us_posting_is_not_rescued_by_a_us_secondary_location() -> None:
    record = {
        "country": "GB",
        "categories": {
            "location": "London",
            "allLocations": ["London", "New York, NY"],
        },
    }

    assert format_location(record) == "London"


def test_a_us_posting_whose_alternates_are_unrecognised_keeps_its_primary() -> None:
    record = {
        "country": "US",
        "categories": {"location": "Philadelphia, PA", "allLocations": ["Anywhere"]},
    }

    assert format_location(record) == "Philadelphia, PA"


def test_a_posting_with_no_location_reports_none() -> None:
    assert format_location({"country": "US", "categories": {}}) is None
    assert format_location({}) is None


async def test_us_and_non_us_postings_both_collect_with_their_own_location(
    stub_boards,
) -> None:
    """The adapter collects; `app.processing.us_locations` decides."""
    stub_boards()

    result = await portal().search(
        a_request("Analytics Engineer", "Junior Operations Supervisor")
    )
    by_title = {job.title: job for job in result.jobs}

    assert by_title["Analytics Engineer"].location == "Philadelphia, PA"
    assert by_title["Junior Operations Supervisor"].location == "Salford, England"


# --- Description --------------------------------------------------------------


def test_the_description_keeps_levers_section_headings(captured) -> None:
    text = build_description(record_by_id(captured, ANALYTICS_ENGINEER))

    assert text is not None
    for heading in (
        "What We Offer",
        "Compensation",
        "Responsibilities",
        "Preferred Qualifications",
        "Minimum Qualifications",
    ):
        assert heading in text, f"{heading} must survive assembly"


def test_the_description_does_not_repeat_the_same_prose_twice() -> None:
    """``descriptionPlain`` already contains ``openingPlain``.

    Measured on live boards: the opening is inside the description on 307 of
    the 307 Palantir postings that carry one, and ``descriptionPlain`` and
    ``descriptionBodyPlain`` are identical on 70 of 71 Spotify postings. Taking
    more than one of them would feed the scorer the same paragraphs twice.
    """
    record = {
        "openingPlain": "A World-Changing Company.",
        "descriptionPlain": "A World-Changing Company. We build software.",
        "descriptionBodyPlain": "A World-Changing Company. We build software.",
        "lists": [],
    }

    text = build_description(record)

    assert text is not None
    assert text.count("A World-Changing Company.") == 1
    assert text.count("We build software.") == 1


def test_list_content_is_flattened_out_of_html() -> None:
    text = build_description(
        {
            "descriptionPlain": "Intro.",
            "lists": [
                {
                    "text": "Responsibilities",
                    "content": "<ul><li>Build models</li><li>Ship &amp; iterate</li></ul>",
                }
            ],
        }
    )

    assert text is not None
    assert "<ul>" not in text and "<li>" not in text
    assert "- Build models" in text
    # Entities are decoded, not left as escapes.
    assert "Ship & iterate" in text
    assert "&amp;" not in text


def test_the_html_description_is_used_when_no_plain_text_twin_exists() -> None:
    text = build_description(
        {"description": "<p>We are hiring an analyst.</p>", "lists": []}
    )

    assert text == "We are hiring an analyst."


def test_the_closing_section_is_kept(captured) -> None:
    record = record_by_id(captured, ANALYTICS_ENGINEER)

    text = build_description(record)
    closing = (record.get("additionalPlain") or "").strip()

    assert text is not None
    if closing:
        assert closing.splitlines()[0] in text


def test_a_posting_with_no_text_at_all_reports_none() -> None:
    assert build_description({}) is None
    assert build_description({"descriptionPlain": "   ", "lists": []}) is None


def test_html_to_text_handles_empty_and_non_string_values() -> None:
    assert html_to_text(None) is None
    assert html_to_text("") is None
    assert html_to_text("   ") is None
    assert html_to_text(123) is None


# --- Workplace type -----------------------------------------------------------


@pytest.mark.parametrize(
    ("published", "expected"),
    [("remote", "Remote"), ("hybrid", "Hybrid"), ("onsite", "Onsite")],
)
def test_the_published_workplace_type_reaches_the_description(
    published: str, expected: str
) -> None:
    """``RawJob`` has no work-mode field, so it goes where the detector reads.

    ``app.processing.titles.detect_work_mode`` scans the description, so
    stating Lever's own value there carries the fact without changing the
    adapter contract for every other portal.
    """
    text = build_description(
        {"descriptionPlain": "Intro.", "workplaceType": published, "lists": []}
    )

    assert text is not None
    assert text.startswith(f"Workplace type: {expected}")


def test_the_published_workplace_type_survives_normalisation(captured) -> None:
    from app.processing.titles import detect_work_mode

    record = record_by_id(captured, US_MULTI)
    assert record["workplaceType"] == "remote"

    text = build_description(record)

    assert text is not None
    assert detect_work_mode(text[:2000]) is not None


@pytest.mark.parametrize("published", ["", None, "flexible", "anything", 7])
def test_an_unrecognised_workplace_type_is_dropped_not_guessed(
    published: Any,
) -> None:
    text = build_description(
        {"descriptionPlain": "Intro.", "workplaceType": published, "lists": []}
    )

    assert text is not None
    assert "Workplace type:" not in text


# --- Employment type ----------------------------------------------------------


@pytest.mark.parametrize(
    ("commitment", "expected"),
    [
        ("Full Time", "Full Time"),
        ("Permanent", "Permanent"),
        ("Full Time/Part Time", "Full Time/Part Time"),
        ("  Part Time  ", "Part Time"),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_the_commitment_is_reported_as_published(
    commitment: Any, expected: str | None
) -> None:
    job = portal()._to_raw_job(
        {
            "id": "x",
            "text": "Analytics Engineer",
            "hostedUrl": "https://jobs.lever.co/gopuff/x",
            "categories": {"commitment": commitment},
        },
        board=LeverBoard("gopuff", "Gopuff"),
    )

    assert job is not None
    assert job.employment_type == expected


def test_a_posting_with_no_categories_has_no_employment_type() -> None:
    job = portal()._to_raw_job(
        {
            "id": "x",
            "text": "Analytics Engineer",
            "hostedUrl": "https://jobs.lever.co/gopuff/x",
        },
        board=LeverBoard("gopuff", "Gopuff"),
    )

    assert job is not None
    assert job.employment_type is None


# --- Board configuration ------------------------------------------------------


def test_the_default_boards_are_used_when_nothing_is_configured() -> None:
    assert LeverPortal().boards[0].as_tuple() == DEFAULT_BOARDS[0]
    assert len(LeverPortal().boards) == len(DEFAULT_BOARDS)


def test_every_default_board_names_its_employer() -> None:
    """A slug is never turned into a company name.

    ``matchgroup`` is "Match Group" and ``fly`` is "Fly.io"; no rule produces
    either. Each name here was read from that board's own schema.org
    ``hiringOrganization``, so it is the employer's own statement.
    """
    for slug, company in DEFAULT_BOARDS:
        assert slug and slug == slug.strip()
        assert company and company == company.strip()

    # Some names legitimately differ from their slug only by capitalisation
    # ("gopuff" -> "Gopuff"), so that alone proves nothing. What proves the
    # names were read rather than generated is that several of them could not
    # be produced from the slug by any rule.
    unguessable = {
        company
        for slug, company in DEFAULT_BOARDS
        if company.casefold() != slug.casefold()
    }
    assert {"Match Group", "Fly.io", "Palantir Technologies"} <= unguessable


def test_boards_may_be_configured_as_mappings() -> None:
    adapter = portal(boards=[{"slug": "acme", "company": "Acme Corp"}])

    assert [board.as_tuple() for board in adapter.boards] == [("acme", "Acme Corp")]


def test_boards_may_be_configured_as_pairs() -> None:
    adapter = portal(boards=[["acme", "Acme Corp"], ("beta", "Beta Inc")])

    assert [board.as_tuple() for board in adapter.boards] == [
        ("acme", "Acme Corp"),
        ("beta", "Beta Inc"),
    ]


def test_a_bare_slug_is_accepted_with_an_unknown_employer() -> None:
    adapter = portal(boards=["acme"])

    assert [board.as_tuple() for board in adapter.boards] == [("acme", None)]


def test_a_malformed_board_entry_is_skipped_not_fatal() -> None:
    adapter = portal(boards=[{"slug": ""}, None, 7, {"slug": "acme"}])

    assert [board.as_tuple() for board in adapter.boards] == [("acme", None)]


def test_an_entirely_unusable_board_list_falls_back_to_the_defaults() -> None:
    adapter = portal(boards=[None, 7, {}])

    assert len(adapter.boards) == len(DEFAULT_BOARDS)


# --- Failure modes ------------------------------------------------------------


async def test_an_empty_board_is_a_success_with_no_jobs(stub_boards) -> None:
    """``200 []`` means "this employer has no openings", not an error.

    Lever distinguishes the two itself: a board with no postings answers
    ``200 []`` while a slug that does not exist answers ``404``. Reading the
    empty one as a failure would report a portal outage every time an employer
    cleared their board.
    """
    stub_boards()
    adapter = portal(boards=[{"slug": "emptyboard", "company": "Empty Co"}])

    result = await adapter.search(a_request("Analytics Engineer"))

    assert result.jobs == []


async def test_one_board_failing_does_not_cost_the_others(stub_boards) -> None:
    stub_boards()
    adapter = portal(
        boards=[
            {"slug": "doesnotexist", "company": "Missing Co"},
            {"slug": "gopuff", "company": "Gopuff"},
        ]
    )

    result = await adapter.search(a_request("Analytics Engineer"))

    assert [job.title for job in result.jobs] == ["Analytics Engineer"]


async def test_every_board_failing_raises_rather_than_reporting_no_jobs(
    stub_boards,
) -> None:
    """An outage must be recorded, not disguised as an empty result set."""
    stub_boards()
    adapter = portal(
        boards=[
            {"slug": "doesnotexist", "company": "A"},
            {"slug": "alsomissing", "company": "B"},
        ]
    )

    with pytest.raises(PortalError) as excinfo:
        await adapter.search(a_request("Analytics Engineer"))

    assert "2 attempted" in excinfo.value.message


async def test_malformed_json_is_a_parse_error(stub_boards) -> None:
    stub_boards(payload_for=lambda url, params: "{not json")

    with pytest.raises(PortalError):
        await portal().search(a_request("Analytics Engineer"))


async def test_a_json_object_instead_of_an_array_is_a_parse_error(
    stub_boards,
) -> None:
    """A changed shape must surface, never be read as "no jobs"."""
    stub_boards(payload_for=lambda url, params: json.dumps({"postings": []}))

    with pytest.raises(PortalError):
        await portal().search(a_request("Analytics Engineer"))

    adapter = portal(boards=[{"slug": "only", "company": "Only"}])
    with pytest.raises(PortalParseError):
        await adapter._board(LeverBoard("only", "Only"))


async def test_non_dict_entries_in_the_array_are_ignored(stub_boards) -> None:
    stub_boards(payload_for=lambda url, params: json.dumps(["nonsense", 7, None]))
    adapter = portal(boards=[{"slug": "odd", "company": "Odd Co"}])

    result = await adapter.search(a_request("Analytics Engineer"))

    assert result.jobs == []


@pytest.mark.parametrize(
    "error",
    [
        PortalRateLimitedError("429", portal="lever"),
        PortalBlockedError("403", portal="lever"),
    ],
)
async def test_a_refusal_stops_the_whole_portal_immediately(
    stub_boards, error: PortalError
) -> None:
    """Moving to the next board would be ignoring what the server just said."""
    calls = stub_boards(payload_for=_raise(error))

    with pytest.raises(type(error)):
        await portal().search(a_request("Analytics Engineer"))

    assert len(calls) == 1, "no further board may be requested after a refusal"


async def test_a_timeout_on_one_board_is_isolated(stub_boards) -> None:
    def _payload(url, params):
        if url.endswith("/spotify"):
            raise PortalTimeoutError("timed out", portal="lever")
        return json.dumps(
            [r for r in json.loads(FIXTURE.read_text(encoding="utf-8"))["gopuff"]]
        )

    stub_boards(payload_for=_payload)

    result = await portal().search(a_request("Analytics Engineer"))

    assert [job.title for job in result.jobs] == ["Analytics Engineer"]


async def test_a_server_error_on_every_board_raises(stub_boards) -> None:
    stub_boards(payload_for=_raise(PortalError("HTTP 503", portal="lever")))

    with pytest.raises(PortalError):
        await portal().search(a_request("Analytics Engineer"))


async def test_health_check_reports_rather_than_raising(stub_boards) -> None:
    stub_boards(payload_for=_raise(PortalError("down", portal="lever")))

    health = await portal().health_check()

    assert health.is_available is False
    assert health.detail is not None


def _raise(error: BaseException):
    def _payload(url, params):  # noqa: ANN001
        raise error

    return _payload


# --- Identity, deduplication and limits ---------------------------------------


async def test_the_same_posting_is_not_collected_twice(stub_boards) -> None:
    doubled = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doubled["gopuff"] = doubled["gopuff"] + doubled["gopuff"]
    stub_boards(boards=doubled)

    result = await portal().search(a_request("Analytics Engineer"))

    assert len(result.jobs) == 1


def test_lever_urls_classify_as_an_ats_destination() -> None:
    """Cross-portal dedup already collapses these; nothing needed changing."""
    hosted = f"https://jobs.lever.co/gopuff/{ANALYTICS_ENGINEER}"

    assert classify_url(hosted).value == "ats"
    assert classify_url(f"{hosted}/apply").value == "ats"


def test_lever_tracking_parameters_are_already_stripped() -> None:
    hosted = f"https://jobs.lever.co/gopuff/{ANALYTICS_ENGINEER}"

    assert canonical_url(f"{hosted}?lever-origin=applied&utm_source=x") == hosted


async def test_the_requested_limit_is_respected(stub_boards) -> None:
    stub_boards()

    result = await portal().search(
        a_request("Analytics Engineer", "Data Scientist", limit=1)
    )

    assert len(result.jobs) == 1


async def test_the_caller_applies_the_freshness_window(stub_boards) -> None:
    """The adapter collects unfiltered and says so."""
    stub_boards()

    result = await portal().search(a_request("Analytics Engineer"))

    assert result.window_applied_by_portal is False
    assert result.has_more is False
    assert result.next_cursor is None


async def test_the_full_text_needs_no_second_request(stub_boards) -> None:
    calls = stub_boards()
    adapter = portal()

    result = await adapter.search(a_request("Analytics Engineer"))
    before = len(calls)
    description = await adapter.fetch_description(result.jobs[0])

    assert description == result.jobs[0].description
    assert len(calls) == before, "fetch_description must not touch the network"


# --- No auto-apply ------------------------------------------------------------


def test_the_adapter_exposes_no_apply_automation() -> None:
    """CLAUDE.md section 1: this project never submits an application."""
    source = (
        Path(__file__).parent.parent
        / "app"
        / "portals"
        / "adapters"
        / "lever.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    for banned in ("easy_apply", "quick_apply", "submit_application", "auto_apply"):
        assert banned not in code.casefold()
