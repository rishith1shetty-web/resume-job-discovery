"""Greenhouse adapter: parsing, board configuration, title matching, failures.

Every response here comes from the captured fixture or a stub. Nothing in this
file makes a network request, and the one path robots.txt disallows - /embed/ -
is asserted never to be requested.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import FreshnessWindow
from app.portals.adapters.greenhouse import (
    API_BASE,
    DEFAULT_BOARD_TOKENS,
    GreenhousePortal,
    html_to_text,
    title_matches,
    title_tokens,
)
from app.portals.base import PortalSearchRequest
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "portals"
    / "greenhouse_financial_analyst.json"
)


@pytest.fixture(scope="module")
def captured() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def records(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """One captured board's postings."""
    return captured["stripe"]["jobs"]


@pytest.fixture
def stub_boards(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]):
    """Serve each board from the fixture, recording every request."""

    def _apply(payload_for=None, boards=captured):
        calls: list[dict[str, Any]] = []

        async def _get_text(self, url, *, params=None):  # noqa: ANN001
            calls.append({"url": url, "params": params or {}})
            if payload_for is not None:
                return payload_for(url, params or {})
            token = url.rstrip("/").split("/")[-2]
            board = boards.get(token)
            if board is None:
                raise PortalError(f"board {token} not found", portal="greenhouse")
            return json.dumps(board)

        monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _get_text)
        return calls

    return _apply


def _portal(**options) -> GreenhousePortal:
    defaults = {"board_tokens": ["stripe", "instacart", "chime"]}
    return GreenhousePortal({**defaults, **options})


async def _search(
    adapter: GreenhousePortal,
    *,
    queries: tuple[str, ...] = ("Financial Analyst",),
    window: FreshnessWindow = FreshnessWindow.LAST_30D,
    limit: int = 50,
    **kwargs,
):
    return await adapter.search(
        PortalSearchRequest(queries=queries, window=window, limit=limit, **kwargs)
    )


# --- Capabilities -----------------------------------------------------------


def test_the_adapter_declares_what_it_can_actually_do() -> None:
    capabilities = _portal().capabilities

    # The full posting text arrives with the listing, so nothing is left to
    # fetch and there are no pages.
    assert capabilities.supports_description_fetch is False
    assert capabilities.supports_pagination is False
    assert capabilities.supports_date_filter is False
    assert capabilities.requires_auth is False
    assert capabilities.provides_application_url is True


def test_the_slug_matches_the_seeded_portal_row() -> None:
    assert GreenhousePortal.slug == "greenhouse"


# --- Access: the one rule robots.txt states ---------------------------------


async def test_only_the_public_board_api_is_requested(stub_boards) -> None:
    """Greenhouse's robots.txt disallows exactly one path: /embed/."""
    calls = stub_boards()

    await _search(_portal())

    assert calls
    for call in calls:
        assert call["url"].startswith(API_BASE)
        assert "/embed/" not in call["url"]


async def test_the_full_posting_text_is_asked_for_explicitly(stub_boards) -> None:
    # Without content=true the API returns titles only, and every listing
    # would then be rejected downstream as unscoreable.
    calls = stub_boards()

    await _search(_portal())

    assert all(call["params"].get("content") == "true" for call in calls)


async def test_no_credentials_are_ever_sent(stub_boards) -> None:
    # The API is documented as public and unauthenticated.
    calls = stub_boards()

    await _search(_portal())

    for call in calls:
        serialised = json.dumps(call["params"]).lower()
        assert "token" not in serialised or "board_token" not in serialised
        assert "authorization" not in serialised
        assert "api_key" not in serialised


# --- Board configuration ----------------------------------------------------


def test_boards_default_when_nothing_is_configured() -> None:
    assert GreenhousePortal()._boards == DEFAULT_BOARD_TOKENS


def test_configured_boards_replace_the_defaults() -> None:
    # Coverage is configuration, not code: adding an employer must not need a
    # deployment.
    adapter = GreenhousePortal({"board_tokens": ["acme", "contoso"]})

    assert adapter._boards == ("acme", "contoso")


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("solo", ("solo",)),
        (["a", "  b  "], ("a", "b")),
        ([], DEFAULT_BOARD_TOKENS),
        (["", "   "], DEFAULT_BOARD_TOKENS),
        (None, DEFAULT_BOARD_TOKENS),
        (42, DEFAULT_BOARD_TOKENS),
    ],
)
def test_board_configuration_is_read_defensively(
    configured: Any, expected: tuple
) -> None:
    adapter = GreenhousePortal({"board_tokens": configured})

    assert adapter._boards == expected


async def test_every_configured_board_is_requested_once(stub_boards) -> None:
    calls = stub_boards()

    await _search(_portal())

    requested = [c["url"] for c in calls]
    assert len(requested) == 3
    for token in ("stripe", "instacart", "chime"):
        assert f"{API_BASE}/{token}/jobs" in requested


async def test_a_board_is_downloaded_once_however_many_queries_run(
    stub_boards,
) -> None:
    """The cache is what keeps this portal cheap.

    Every same-role variation filters the same boards, so without it a
    six-query plan would download every board six times.
    """
    calls = stub_boards()
    adapter = _portal()

    await _search(
        adapter,
        queries=("Financial Analyst", "Senior Financial Analyst", "FP&A Analyst"),
    )

    assert len(calls) == 3  # one per board, not one per board per query


# --- Title matching ---------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("Financial Analyst", "Financial Analyst"),
        ("Financial Analyst", "Senior Financial Analyst"),
        ("Financial Analyst", "Financial Analyst, Business F&S"),
        ("Financial Analyst", "Staff Financial Analyst (Remote)"),
        ("FP&A Analyst", "FP&A Analyst"),
        ("financial analyst", "FINANCIAL ANALYST II"),
    ],
)
def test_a_title_answering_the_query_is_collected(query: str, title: str) -> None:
    assert title_matches(query, title) is True


@pytest.mark.parametrize(
    ("query", "title"),
    [
        ("Financial Analyst", "Business Analyst"),
        ("Financial Analyst", "Data Analyst"),
        ("Financial Analyst", "Finance Manager"),
        ("FP&A Analyst", "Financial Analyst"),
        ("Financial Analyst", "Software Engineer"),
    ],
)
def test_a_title_that_does_not_answer_the_query_is_skipped(
    query: str, title: str
) -> None:
    assert title_matches(query, title) is False


@pytest.mark.parametrize("value", ["", None, "   ", "&&&"])
def test_an_empty_query_matches_nothing(value: str | None) -> None:
    # Matching everything would flood the pipeline with unrelated roles.
    assert title_matches(value or "", "Financial Analyst") is False


def test_single_letters_are_not_significant_tokens() -> None:
    # "FP&A" must require "fp", not "a", which appears in almost every title.
    assert title_tokens("FP&A Analyst") == frozenset({"fp", "analyst"})


async def test_only_matching_titles_are_returned(stub_boards, captured) -> None:
    stub_boards()

    result = await _search(_portal())

    assert result.jobs
    for job in result.jobs:
        assert "financial" in job.title.casefold()
        assert "analyst" in job.title.casefold()


async def test_several_queries_widen_what_is_collected(stub_boards) -> None:
    stub_boards()

    narrow = await _search(_portal(), queries=("Financial Analyst",))
    wide = await _search(
        _portal(), queries=("Financial Analyst", "Software Engineer")
    )

    assert len(wide.jobs) >= len(narrow.jobs)


# --- Parsing ----------------------------------------------------------------


async def test_listings_are_mapped_onto_the_shared_contract(
    stub_boards, captured
) -> None:
    stub_boards()

    result = await _search(_portal())

    assert result.jobs
    job = result.jobs[0]
    assert job.source == "greenhouse"
    assert job.title
    assert job.company
    assert job.external_id
    assert job.source_url.startswith("http")
    assert job.application_url == job.source_url


async def test_the_employer_is_taken_from_the_posting_not_the_board_token(
    stub_boards, captured
) -> None:
    # The board token is how the board was addressed, not who is hiring.
    stub_boards()

    result = await _search(_portal())

    companies = {job.company for job in result.jobs}
    assert "stripe" not in companies
    assert any(company and company[0].isupper() for company in companies)


async def test_the_complete_posting_text_is_carried(stub_boards) -> None:
    stub_boards()

    result = await _search(_portal())

    assert result.jobs
    for job in result.jobs:
        assert job.description
        # The employer's own prose, not a summary or a stub.
        assert len(job.description) > 500
        # Markup must not survive into the scored text.
        assert "<p>" not in job.description
        assert "&lt;" not in job.description


async def test_the_posted_date_comes_from_the_publication_timestamp(
    stub_boards, records
) -> None:
    stub_boards()

    result = await _search(_portal())

    assert result.jobs
    by_id = {job.external_id: job for job in result.jobs}
    for record in records:
        job = by_id.get(str(record["id"]))
        if job is None:
            continue
        assert job.posted_at is not None
        assert job.posted_at.tzinfo is not None


@pytest.mark.parametrize(
    "value", [None, "", "   ", "not a date", 1784922724873, True]
)
def test_an_unusable_timestamp_leaves_the_date_empty(value: Any) -> None:
    # Better an empty date than a made-up one: freshness ranking reads this.
    assert GreenhousePortal._posted_at(value) is None


def test_a_naive_timestamp_is_treated_as_utc() -> None:
    assert GreenhousePortal._posted_at("2026-07-15T01:44:23") == datetime(
        2026, 7, 15, 1, 44, 23, tzinfo=UTC
    )


def test_the_location_falls_back_to_the_first_named_office() -> None:
    assert (
        GreenhousePortal._location(
            {"location": None, "offices": [{"name": "Remote - US"}]}
        )
        == "Remote - US"
    )
    assert GreenhousePortal._location({"location": {"name": "Dublin"}}) == "Dublin"
    assert GreenhousePortal._location({}) is None


def test_employment_type_is_read_from_board_metadata_when_present() -> None:
    # Greenhouse has no standard field for it.
    assert (
        GreenhousePortal._employment_type(
            {"metadata": [{"name": "Employment Type", "value": "Full-time"}]}
        )
        == "Full-time"
    )
    assert (
        GreenhousePortal._employment_type(
            {"metadata": [{"name": "Career Page Bucket", "value": "Finance"}]}
        )
        is None
    )
    assert GreenhousePortal._employment_type({"metadata": None}) is None


async def test_the_board_payload_survives_for_later_re_extraction(
    stub_boards,
) -> None:
    stub_boards()

    result = await _search(_portal())

    assert all("id" in job.raw_payload for job in result.jobs)
    assert all("absolute_url" in job.raw_payload for job in result.jobs)


# --- HTML flattening --------------------------------------------------------


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("&lt;p&gt;One&lt;/p&gt;&lt;p&gt;Two&lt;/p&gt;", "One\nTwo"),
        ("&lt;ul&gt;&lt;li&gt;First&lt;/li&gt;&lt;/ul&gt;", "- First"),
        ("Line&lt;br/&gt;Break", "Line\nBreak"),
        ("&lt;p&gt;P&amp;amp;L&lt;/p&gt;", "P&L"),
    ],
)
def test_escaped_posting_html_becomes_readable_text(
    markup: str, expected: str
) -> None:
    # Greenhouse escapes the markup inside the JSON, so it is unescaped once to
    # recover the HTML and then flattened.
    assert html_to_text(markup) == expected


@pytest.mark.parametrize("value", [None, "", "   ", 42, [], {}])
def test_empty_or_non_text_markup_yields_nothing(value: Any) -> None:
    assert html_to_text(value) is None


# --- Malformed payloads -----------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["not json at all", '{"jobs": "not a list"}', "{}", "[]"],
)
async def test_a_malformed_board_payload_is_reported_not_crashed(
    stub_boards, body: str
) -> None:
    # Every board is malformed here, so the portal itself has failed.
    stub_boards(payload_for=lambda url, params: body)

    with pytest.raises(PortalError) as exc_info:
        await _search(_portal())

    assert exc_info.value.portal == "greenhouse"


async def test_a_board_with_no_openings_is_not_an_error(stub_boards) -> None:
    stub_boards(payload_for=lambda url, params: json.dumps({"jobs": []}))

    result = await _search(_portal())

    assert result.jobs == []


async def test_a_posting_without_a_link_is_skipped_not_returned(
    stub_boards,
) -> None:
    stub_boards(
        payload_for=lambda url, params: json.dumps(
            {
                "jobs": [
                    {"id": 1, "title": "Financial Analyst"},
                    {
                        "id": 2,
                        "title": "Financial Analyst",
                        "absolute_url": "https://example.com/jobs/2",
                        "content": "&lt;p&gt;Body&lt;/p&gt;",
                    },
                ]
            }
        )
    )

    result = await _search(_portal())

    assert len(result.jobs) == 1
    assert result.jobs[0].external_id == "2"


async def test_a_non_dict_posting_cannot_break_the_batch(stub_boards) -> None:
    stub_boards(
        payload_for=lambda url, params: json.dumps({"jobs": ["nope", None, 7]})
    )

    result = await _search(_portal())

    assert result.jobs == []


async def test_the_same_posting_is_never_collected_twice(stub_boards) -> None:
    stub_boards()

    result = await _search(
        _portal(), queries=("Financial Analyst", "Financial Analyst")
    )

    ids = [job.external_id for job in result.jobs]
    assert len(ids) == len(set(ids))


async def test_collection_stops_at_the_requested_limit(stub_boards) -> None:
    stub_boards()

    result = await _search(_portal(), limit=2)

    assert len(result.jobs) == 2


async def test_the_caller_is_told_to_apply_the_freshness_window(
    stub_boards,
) -> None:
    stub_boards()

    result = await _search(_portal())

    assert result.window_applied_by_portal is False


# --- Per-board failure isolation --------------------------------------------


async def test_one_unreachable_board_does_not_lose_the_others(
    stub_boards, captured
) -> None:
    """A single employer's board being down costs that employer, not the run."""

    def payload_for(url: str, params: dict) -> str:
        token = url.rstrip("/").split("/")[-2]
        if token == "stripe":
            raise PortalError("board is down", portal="greenhouse")
        return json.dumps(captured[token])

    stub_boards(payload_for=payload_for)

    result = await _search(_portal())

    # instacart still contributed.
    assert result.jobs
    assert all(job.company != "Stripe" for job in result.jobs)


async def test_every_board_failing_is_reported_as_a_portal_failure(
    stub_boards,
) -> None:
    # Returning an empty result would read as "Greenhouse has no matching
    # jobs", which is a materially different and misleading claim.
    def payload_for(url: str, params: dict) -> str:
        raise PortalError("board is down", portal="greenhouse")

    stub_boards(payload_for=payload_for)

    with pytest.raises(PortalError) as exc_info:
        await _search(_portal())

    assert "No Greenhouse board could be read" in exc_info.value.message


@pytest.mark.parametrize(
    "error",
    [
        PortalBlockedError("refused", portal="greenhouse"),
        PortalRateLimitedError("slow down", portal="greenhouse"),
    ],
)
async def test_a_refusal_stops_the_whole_portal_immediately(
    stub_boards, error: PortalError
) -> None:
    """Moving to the next board would be ignoring what the API just said."""
    calls = stub_boards(payload_for=lambda url, params: (_ for _ in ()).throw(error))

    with pytest.raises(type(error)):
        await _search(_portal())

    # It stopped at the first board rather than trying the rest.
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        PortalTimeoutError("timed out", portal="greenhouse"),
        PortalError("network", portal="greenhouse"),
    ],
)
async def test_a_transport_failure_on_every_board_is_raised(
    stub_boards, error: PortalError
) -> None:
    stub_boards(payload_for=lambda url, params: (_ for _ in ()).throw(error))

    with pytest.raises(PortalError):
        await _search(_portal())


async def test_the_configured_timeout_and_retries_are_honoured() -> None:
    # Operational policy comes from the portal_configs row, not from code.
    adapter = GreenhousePortal(
        {"timeout_seconds": 12.0, "max_retries": 4, "rate_limit_per_minute": 15}
    )

    assert adapter._http.policy.timeout_seconds == 12.0
    assert adapter._http.policy.max_retries == 4
    assert adapter._http.policy.rate_limit_per_minute == 15


async def test_an_unknown_option_does_not_break_construction() -> None:
    adapter = GreenhousePortal({"some_future_option": True})

    assert adapter.capabilities.requires_auth is False


async def test_a_health_check_reports_failure_rather_than_raising(
    stub_boards,
) -> None:
    stub_boards(
        payload_for=lambda url, params: (_ for _ in ()).throw(
            PortalTimeoutError("timed out", portal="greenhouse")
        )
    )

    health = await _portal().health_check()

    assert health.is_available is False
    assert health.detail is not None


async def test_a_health_check_passes_when_the_api_answers(stub_boards) -> None:
    stub_boards()

    health = await _portal().health_check()

    assert health.is_available is True


async def test_fetching_a_description_needs_no_second_request(
    stub_boards,
) -> None:
    calls = stub_boards()
    adapter = _portal()
    result = await _search(adapter)

    before = len(calls)
    text = await adapter.fetch_description(result.jobs[0])

    assert text == result.jobs[0].description
    assert len(calls) == before


# --- The canonical-key collision this portal exposed ------------------------


def test_a_repeat_opening_keeps_its_own_key_when_the_first_holds_the_shared_one(
) -> None:
    """Regression guard for a defect Greenhouse surfaced live.

    ``build_canonical_key`` is deliberately date-free, so one employer's repeat
    opening for the same role and place produces the same key. ``compare``
    refuses to merge probable duplicates more than 45 days apart. Where those
    meet, the insert used to fail on uq_jobs_canonical_key - Stripe had three
    "Risk Operations Analyst, Financial Crimes" openings, two in Dublin 250
    days apart.
    """
    from app.portals.base import RawJob
    from app.processing.normalize import normalize_raw_job
    from app.services.job_processing import _available_key

    def normalised(external_id: str):
        return normalize_raw_job(
            RawJob(
                external_id=external_id,
                source="greenhouse",
                title="Risk Operations Analyst, Financial Crimes",
                company="Stripe",
                location="Dublin",
                description="x" * 400,
                source_url=f"https://boards.greenhouse.io/stripe/jobs/{external_id}",
                application_url=None,
                posted_at=None,
                employment_type=None,
            )
        )

    first = normalised("7430332")
    second = normalised("7974211")

    # Same employer, title and place: the shared key is the whole point.
    assert first.canonical_key == second.canonical_key

    # Nothing is taken, so the key is used unchanged - ordinary behaviour.
    assert _available_key(first, set()) == first.canonical_key

    # Once the first holds it, the second takes a distinguished key rather
    # than colliding, and never merges into a job dedup rejected.
    distinguished = _available_key(second, {first.canonical_key})
    assert distinguished != second.canonical_key

    # Stable: the same repeat opening rediscovered later resolves to the same
    # key rather than adding a row on every search.
    assert distinguished == _available_key(second, {first.canonical_key})

    # And two different repeat openings do not collapse onto one another.
    third = normalised("7974209")
    assert distinguished != _available_key(third, {first.canonical_key})
