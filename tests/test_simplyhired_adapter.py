"""SimplyHired adapter: parsing, URLs, pagination, retries, malformed pages.

Every response here comes from the captured fixture or a stub. Nothing in this
file makes a network request, and the access rules the adapter exists to
respect are asserted directly: the disallowed API surface and the disallowed
click redirect must never be requested.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import FreshnessWindow
from app.portals.adapters.simplyhired import (
    BASE_URL,
    DEFAULT_LOCATION,
    SimplyHiredPortal,
    html_to_text,
)
from app.portals.base import PortalSearchRequest, RawJob
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
    / "simplyhired_financial_analyst.json"
)


@pytest.fixture(scope="module")
def captured() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def pages(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """Both captured pages of the "financial analyst" search."""
    return captured["searches"]["financial analyst"]


@pytest.fixture
def records(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The first captured page: twenty real listings."""
    return pages[0]["jobs"]


def _page(props: dict[str, Any]) -> str:
    """Wrap page props the way the live site embeds them."""
    inner = json.dumps({"props": {"pageProps": props}})
    return f'<script id="__NEXT_DATA__" type="application/json">{inner}</script>'


@pytest.fixture
def stub_http(monkeypatch: pytest.MonkeyPatch):
    """Serve canned pages, recording every request the adapter makes."""

    def _apply(handler):
        calls: list[dict[str, Any]] = []

        async def _get_text(self, url, *, params=None):  # noqa: ANN001
            calls.append({"url": url, "params": params or {}})
            return handler(len(calls), url, params or {})

        monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _get_text)
        return calls

    return _apply


def _search_handler(page_list: list[dict[str, Any]]):
    """Answer each successive search request with the next captured page."""
    state = {"index": 0}

    def handler(call: int, url: str, params: dict) -> str:
        index = min(state["index"], len(page_list) - 1)
        state["index"] += 1
        return _page(page_list[index])

    return handler


async def _search(
    adapter: SimplyHiredPortal,
    *,
    queries: tuple[str, ...] = ("Financial Analyst",),
    window: FreshnessWindow = FreshnessWindow.LAST_30D,
    limit: int = 60,
    **kwargs,
):
    return await adapter.search(
        PortalSearchRequest(queries=queries, window=window, limit=limit, **kwargs)
    )


# --- Capabilities -----------------------------------------------------------


def test_the_adapter_declares_what_it_can_actually_do() -> None:
    capabilities = SimplyHiredPortal().capabilities

    # `/job/<jobKey>` really does carry the employer's full posting text.
    assert capabilities.supports_description_fetch is True
    assert capabilities.supports_pagination is True
    # The search page carries no date filter this adapter trusts.
    assert capabilities.supports_date_filter is False
    assert capabilities.requires_auth is False


def test_the_slug_matches_the_seeded_portal_row() -> None:
    assert SimplyHiredPortal.slug == "simplyhired"


# --- Access: the rules this adapter exists to respect -----------------------


async def test_only_robots_allowed_paths_are_requested(stub_http, pages) -> None:
    """robots.txt closes a specific list of paths to this client.

    `/c/jobs-api/`, `/serp`, `/job-id/`, `/job-post/` and `/out?r=` are all
    disallowed. Only `/search` and `/job/` are read.
    """
    calls = stub_http(_search_handler(pages))

    await _search(SimplyHiredPortal())

    assert calls, "the adapter made no request at all"
    for call in calls:
        url = call["url"]
        assert url.startswith(f"{BASE_URL}/search") or url.startswith(
            f"{BASE_URL}/job/"
        ), url
        for forbidden in (
            "/c/jobs-api/",
            "/c/jobs-widget/",
            "/serp",
            "/job-id/",
            "/job-post/",
            "/out?r=",
            "/a/job-details/",
        ):
            assert forbidden not in url


async def test_the_disallowed_click_redirect_is_never_used_as_a_link(
    stub_http, pages, records
) -> None:
    # `encodedJobClickPingUrl` and `encodedUrl` point at `/out?r=`, which is
    # disallowed, and carry per-card tracking. The crawler-facing job URL is
    # used instead.
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal())

    for job in result.jobs:
        assert "/out?r=" not in job.source_url
        assert "jobCardTrackingKey" not in job.source_url
        assert "xkcb" not in job.source_url
        assert job.source_url.startswith(f"{BASE_URL}/job/")


# --- URL generation ---------------------------------------------------------


async def test_the_query_and_location_are_sent_as_search_parameters(
    stub_http, pages
) -> None:
    calls = stub_http(_search_handler(pages))

    await _search(SimplyHiredPortal(), queries=("FP&A Analyst",), limit=5)

    assert calls[0]["url"] == f"{BASE_URL}/search"
    assert calls[0]["params"]["q"] == "FP&A Analyst"
    assert calls[0]["params"]["l"] == DEFAULT_LOCATION


async def test_a_caller_supplied_location_is_honoured(stub_http, pages) -> None:
    calls = stub_http(_search_handler(pages))

    await _search(SimplyHiredPortal(), limit=5, location="Austin, TX")

    assert calls[0]["params"]["l"] == "Austin, TX"


async def test_a_remote_only_request_is_passed_through(stub_http, pages) -> None:
    calls = stub_http(_search_handler(pages))

    await _search(SimplyHiredPortal(), limit=5, remote_only=True)

    assert calls[0]["params"].get("rl") == "Remote"


async def test_the_job_url_is_built_from_the_stable_key(stub_http, pages, records):
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal())

    by_key = {job.external_id: job for job in result.jobs}
    for record in records:
        job = by_key[record["jobKey"]]
        assert job.source_url == f"{BASE_URL}/job/{record['jobKey']}"
        # Nothing to apply through but the listing itself.
        assert job.application_url == job.source_url


# --- Parsing ----------------------------------------------------------------


async def test_listings_are_mapped_onto_the_shared_contract(
    stub_http, pages, records
) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    assert len(result.jobs) == len(records)
    job = result.jobs[0]
    assert job.source == "simplyhired"
    assert job.title == records[0]["title"]
    assert job.company == records[0]["company"]
    assert job.location == records[0]["location"]
    assert job.external_id == records[0]["jobKey"]


async def test_every_captured_listing_survives_parsing(
    stub_http, pages, records
) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    assert all(job.title for job in result.jobs)
    assert all(job.company for job in result.jobs)
    assert all(job.location for job in result.jobs)
    assert all(job.posted_at is not None for job in result.jobs)
    # Distinct vacancies must not collapse onto one key.
    assert len({job.external_id for job in result.jobs}) == len(records)


async def test_the_employment_type_comes_from_the_job_types(
    stub_http, pages, records
) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    by_key = {job.external_id: job for job in result.jobs}
    for record in records:
        types = record.get("jobTypes") or []
        expected = types[0] if types else None
        assert by_key[record["jobKey"]].employment_type == expected


async def test_the_posted_date_comes_from_the_sites_own_timestamp(
    stub_http, pages, records
) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    by_key = {job.external_id: job for job in result.jobs}
    for record in records:
        expected = datetime.fromtimestamp(record["dateOnIndeed"] / 1000, tz=UTC)
        assert by_key[record["jobKey"]].posted_at == expected


@pytest.mark.parametrize("value", [None, "", "1784922724873", -1, 0, True, False])
def test_an_unusable_timestamp_leaves_the_date_empty(value: Any) -> None:
    # Better an empty date than a made-up one: freshness ranking reads this.
    assert SimplyHiredPortal._posted_at(value) is None


async def test_salary_and_work_arrangement_survive_in_the_payload(
    stub_http, pages
) -> None:
    # RawJob carries neither, so they stay in the payload rather than being
    # written into a field they do not belong in.
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    assert any("salaryInfo" in job.raw_payload for job in result.jobs)
    assert all("remoteAttributes" in job.raw_payload for job in result.jobs)


async def test_a_remote_listing_says_so_in_its_location(stub_http, pages) -> None:
    """Work mode arrives through the field that already carries it.

    The site puts "Remote" in ``location`` itself, so normalisation picks the
    work mode up without the adapter inventing a field for it.
    """
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    assert any((job.location or "").lower() == "remote" for job in result.jobs)


# --- The composed description ----------------------------------------------


async def test_a_description_is_composed_from_the_published_fields(
    stub_http, pages
) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    described = [job for job in result.jobs if job.description]
    assert described, "no listing produced a description at all"

    body = described[0].description or ""
    # Long enough for the matching layer to treat as scoreable.
    assert len(body) >= 200
    assert "Requirements:" in body


async def test_the_requirements_section_carries_the_sites_own_list(
    stub_http, pages, records
) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    by_key = {job.external_id: job for job in result.jobs}
    record = next(r for r in records if r.get("requirements"))
    body = by_key[record["jobKey"]].description or ""

    for requirement in record["requirements"]:
        assert requirement in body


async def test_unclassified_phrases_are_not_presented_as_requirements(
    stub_http, pages, records
) -> None:
    """``uncategorized`` is Indeed's own label for unclassified extractions.

    Filing them under "Requirements" would manufacture mandatory criteria the
    posting never stated, which the matching layer would then hold the resume
    to.
    """
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=20)

    by_key = {job.external_id: job for job in result.jobs}
    record = next(r for r in records if r.get("uncategorized"))
    body = by_key[record["jobKey"]].description or ""

    requirements_block = body.split("Requirements:", 1)[1].split("\n\n", 1)[0]
    for phrase in record["uncategorized"]:
        assert phrase not in requirements_block
    assert "Also mentioned in this posting:" in body


def test_a_listing_with_nothing_to_say_gets_no_description() -> None:
    # A stub would look like a real posting to the matching layer.
    assert SimplyHiredPortal._description({"title": "Analyst"}) is None


def test_html_entities_are_unescaped_in_composed_text() -> None:
    body = SimplyHiredPortal._description(
        {
            "snippet": "Manage P&amp;L and reporting for the finance team.",
            "requirements": ["Bachelor&#39;s degree", "SQL"],
            "benefits": ["401(k)"],
            "uncategorized": ["Variance analysis", "Budgeting", "Forecasting"],
        }
    )

    assert body is not None
    assert "P&L" in body
    assert "&amp;" not in body
    assert "&#39;" not in body


# --- Pagination -------------------------------------------------------------


async def test_a_second_page_is_requested_with_the_published_cursor(
    stub_http, pages
) -> None:
    calls = stub_http(_search_handler(pages))

    await _search(SimplyHiredPortal(), limit=60)

    assert len(calls) >= 2
    assert "cursor" not in calls[0]["params"]
    assert calls[1]["params"]["cursor"] == pages[0]["pageCursors"]["2"]


async def test_both_pages_of_listings_are_collected(stub_http, pages) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=60)

    expected = len(pages[0]["jobs"]) + len(pages[1]["jobs"])
    assert len(result.jobs) == expected
    # Page two must not repeat page one.
    assert len({job.external_id for job in result.jobs}) == expected


async def test_paging_stops_when_no_further_cursor_is_published(
    stub_http, records
) -> None:
    # The last page of a result set publishes no cursor for the page after it,
    # so there is nothing to ask for and the adapter must not guess one.
    last_page = {
        "jobs": records,
        "pageCursors": {"1": "cursor-for-page-one"},
        "currentPageNumber": 1,
    }
    calls = stub_http(lambda call, url, params: _page(last_page))

    result = await _search(SimplyHiredPortal(), limit=1000)

    assert len(calls) == 1
    assert len(result.jobs) == len(records)


async def test_collection_stops_at_the_requested_limit(stub_http, pages) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal(), limit=7)

    assert len(result.jobs) == 7


async def test_paging_is_bounded_however_many_cursors_exist(
    stub_http, pages
) -> None:
    # The site publishes cursors far beyond what any quota needs; the adapter
    # must not walk them all and spend the whole run's budget.
    calls = stub_http(_search_handler([pages[0], pages[0]]))

    await _search(SimplyHiredPortal(), limit=1000)

    searches = [c for c in calls if c["url"].endswith("/search")]
    assert len(searches) <= 2


async def test_each_query_is_searched_independently(stub_http, pages) -> None:
    calls = stub_http(_search_handler(pages))

    await _search(
        SimplyHiredPortal(),
        queries=("Financial Analyst", "FP&A Analyst"),
        limit=1000,
    )

    roles = [c["params"].get("q") for c in calls]
    assert "Financial Analyst" in roles
    assert "FP&A Analyst" in roles


async def test_the_same_listing_twice_is_collected_once(stub_http, pages) -> None:
    stub_http(_search_handler([pages[0], pages[0]]))

    result = await _search(
        SimplyHiredPortal(), queries=("A", "B"), limit=1000
    )

    ids = [job.external_id for job in result.jobs]
    assert len(ids) == len(set(ids))


async def test_the_caller_is_told_to_apply_the_freshness_window_itself(
    stub_http, pages
) -> None:
    stub_http(_search_handler(pages))

    result = await _search(SimplyHiredPortal())

    assert result.window_applied_by_portal is False


# --- Full description on demand ---------------------------------------------


async def test_the_full_posting_text_is_read_from_the_job_page(
    stub_http, captured
) -> None:
    detail = captured["detail"]

    def handler(call: int, url: str, params: dict) -> str:
        assert url == f"{BASE_URL}/job/{detail['jobKey']}"
        return _page(detail)

    stub_http(handler)

    job = RawJob(
        external_id=detail["jobKey"],
        source="simplyhired",
        title="Financial Analyst",
        company="Eventeny",
        location="Remote",
        description="composed summary",
        source_url=f"{BASE_URL}/job/{detail['jobKey']}",
        application_url=None,
        posted_at=None,
        employment_type=None,
    )

    text = await SimplyHiredPortal().fetch_description(job)

    assert text is not None
    # The employer's own prose, not the composed summary.
    assert len(text) > 1000
    assert "<" not in text
    assert "Eventeny" in text


async def test_an_expired_listing_keeps_what_was_already_collected(
    stub_http, captured
) -> None:
    detail = dict(captured["detail"])
    detail["expired"] = True

    stub_http(lambda call, url, params: _page(detail))

    job = RawJob(
        external_id=detail["jobKey"],
        source="simplyhired",
        title="Financial Analyst",
        company="Eventeny",
        location="Remote",
        description="composed summary",
        source_url=f"{BASE_URL}/job/{detail['jobKey']}",
        application_url=None,
        posted_at=None,
        employment_type=None,
    )

    assert await SimplyHiredPortal().fetch_description(job) == "composed summary"


async def test_a_missing_job_page_is_not_an_adapter_failure(
    stub_http,
) -> None:
    # A listing that has since gone is not a portal breaking.
    stub_http(lambda call, url, params: "<html>no data here</html>")

    job = RawJob(
        external_id="gone",
        source="simplyhired",
        title="Financial Analyst",
        company="Acme",
        location="Remote",
        description="composed summary",
        source_url=f"{BASE_URL}/job/gone",
        application_url=None,
        posted_at=None,
        employment_type=None,
    )

    assert await SimplyHiredPortal().fetch_description(job) == "composed summary"


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("<p>One</p><p>Two</p>", "One\nTwo"),
        ("<ul><li>First</li><li>Second</li></ul>", "- First\n- Second"),
        ("Line<br/>Break", "Line\nBreak"),
        ("<p>P&amp;L &lt;analysis&gt;</p>", "P&L <analysis>"),
    ],
)
def test_posting_html_becomes_readable_text(markup: str, expected: str) -> None:
    # Headings and list items must not run together: the matching layer reads
    # those boundaries to tell a stated requirement from prose.
    assert html_to_text(markup) == expected


@pytest.mark.parametrize("value", [None, "", "   ", 42, [], {}])
def test_empty_or_non_text_markup_yields_nothing(value: Any) -> None:
    assert html_to_text(value) is None


# --- Malformed and hostile pages --------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "<html>no next data</html>",
        '<script id="__NEXT_DATA__" type="application/json">{not json}</script>',
        '<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>',
        '<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{}}}</script>',
    ],
)
async def test_a_malformed_page_is_a_portal_error_not_a_crash(
    stub_http, body: str
) -> None:
    stub_http(lambda call, url, params: body)

    with pytest.raises(PortalParseError) as exc_info:
        await _search(SimplyHiredPortal())

    assert exc_info.value.portal == "simplyhired"


async def test_results_that_are_not_a_list_are_rejected(stub_http) -> None:
    stub_http(lambda call, url, params: _page({"jobs": "not a list"}))

    with pytest.raises(PortalParseError):
        await _search(SimplyHiredPortal())


async def test_an_empty_result_set_is_not_an_error(stub_http) -> None:
    # A role with no matches is a legitimate answer, not a failure.
    stub_http(lambda call, url, params: _page({"jobs": []}))

    result = await _search(SimplyHiredPortal())

    assert result.jobs == []


async def test_a_card_without_a_key_is_skipped_not_returned(stub_http) -> None:
    stub_http(
        lambda call, url, params: _page(
            {
                "jobs": [
                    {"title": "Financial Analyst"},
                    {"jobKey": "abc", "title": "Financial Analyst", "company": "Acme"},
                ]
            }
        )
    )

    result = await _search(SimplyHiredPortal())

    assert len(result.jobs) == 1
    assert result.jobs[0].external_id == "abc"


async def test_a_card_without_a_title_is_skipped(stub_http) -> None:
    stub_http(
        lambda call, url, params: _page({"jobs": [{"jobKey": "abc", "title": "  "}]})
    )

    result = await _search(SimplyHiredPortal())

    assert result.jobs == []


async def test_a_non_dict_card_cannot_break_the_batch(stub_http) -> None:
    stub_http(lambda call, url, params: _page({"jobs": ["nope", None, 7]}))

    result = await _search(SimplyHiredPortal())

    assert result.jobs == []


async def test_a_malformed_cursor_block_just_ends_pagination(
    stub_http, records
) -> None:
    stub_http(
        lambda call, url, params: _page(
            {"jobs": records, "pageCursors": "not a dict", "currentPageNumber": 1}
        )
    )

    result = await _search(SimplyHiredPortal(), limit=1000)

    assert len(result.jobs) == len(records)


# --- Transport failures -----------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        PortalTimeoutError("timed out", portal="simplyhired"),
        PortalRateLimitedError("slow down", portal="simplyhired"),
        PortalBlockedError("refused", portal="simplyhired"),
        PortalError("network", portal="simplyhired"),
    ],
)
async def test_a_transport_failure_is_raised_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, error: PortalError
) -> None:
    """An empty result would read as "this portal has no matching jobs".

    That is a materially different and misleading claim, so the failure is
    raised and the run records it against this portal alone.
    """

    async def _get_text(self, url, *, params=None):  # noqa: ANN001
        raise error

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _get_text)

    with pytest.raises(type(error)):
        await _search(SimplyHiredPortal())


async def test_the_configured_timeout_and_retries_are_honoured() -> None:
    # Operational policy comes from the portal_configs row, not from code.
    adapter = SimplyHiredPortal(
        {"timeout_seconds": 8.5, "max_retries": 3, "rate_limit_per_minute": 20}
    )

    assert adapter._http.policy.timeout_seconds == 8.5
    assert adapter._http.policy.max_retries == 3
    assert adapter._http.policy.rate_limit_per_minute == 20


async def test_an_unknown_option_does_not_break_construction() -> None:
    adapter = SimplyHiredPortal({"some_future_option": True})

    assert adapter.capabilities.requires_auth is False


async def test_a_health_check_reports_failure_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _head_ok(self, url):  # noqa: ANN001
        return False

    monkeypatch.setattr("app.portals.http.PortalHttpClient.head_ok", _head_ok)

    health = await SimplyHiredPortal().health_check()

    assert health.is_available is False
    assert health.detail is not None


async def test_a_health_check_passes_when_the_site_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _head_ok(self, url):  # noqa: ANN001
        return True

    monkeypatch.setattr("app.portals.http.PortalHttpClient.head_ok", _head_ok)

    health = await SimplyHiredPortal().health_check()

    assert health.is_available is True
