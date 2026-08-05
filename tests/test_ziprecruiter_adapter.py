"""ZipRecruiter adapter: parsing, URLs, retries, timeouts, malformed payloads.

Every response here comes from the captured fixture or a stub. Nothing in this
file makes a network request, and the access rule the adapter exists to respect
is asserted directly: no ziprecruiter.com page may ever be fetched, because
robots.txt closes the whole site to this client.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import FreshnessWindow
from app.portals.adapters.ziprecruiter import (
    MCP_ENDPOINT,
    ZipRecruiterPortal,
    stable_job_key,
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
    / "ziprecruiter_financial_analyst.json"
)


@pytest.fixture(scope="module")
def captured() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def records(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """The first captured page: five real listings."""
    return captured["Financial Analyst"][0]["results"]


def _envelope(results: list[dict[str, Any]], *, warnings: list | None = None) -> dict:
    """Wrap records the way the MCP tool returns them."""
    return {
        "result": {
            "structuredContent": {
                "request": {},
                "results": results,
                "status": "OK",
                "meta": {"count": len(results), "limit": 5, "total": 1155},
                "warnings": warnings or [],
            }
        }
    }


@pytest.fixture
def stub_mcp(monkeypatch: pytest.MonkeyPatch):
    """Drive the adapter from canned MCP responses, recording every call."""

    def _apply(handler):
        calls: list[dict[str, Any]] = []

        async def _post_json(self, url, *, payload, headers=None):  # noqa: ANN001
            calls.append({"url": url, "payload": payload})
            return handler(len(calls), payload)

        monkeypatch.setattr(
            "app.portals.http.PortalHttpClient.post_json", _post_json
        )
        return calls

    return _apply


def _handler_for(pages: list[list[dict[str, Any]]]):
    """Answer initialize, then hand out pages in order."""
    state = {"page": 0}

    def handler(call: int, payload: dict) -> dict:
        if payload["method"] == "initialize":
            return {"result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        index = min(state["page"], len(pages) - 1)
        state["page"] += 1
        return _envelope(pages[index])

    return handler


async def _search(
    adapter: ZipRecruiterPortal,
    *,
    queries: tuple[str, ...] = ("Financial Analyst",),
    window: FreshnessWindow = FreshnessWindow.LAST_30D,
    limit: int = 20,
    **kwargs,
):
    return await adapter.search(
        PortalSearchRequest(queries=queries, window=window, limit=limit, **kwargs)
    )


# --- Capabilities -----------------------------------------------------------


def test_the_adapter_declares_what_it_can_actually_do() -> None:
    capabilities = ZipRecruiterPortal().capabilities

    # Collected unfiltered and filtered locally; see the collection tests.
    assert capabilities.supports_date_filter is False
    assert capabilities.supports_pagination is True
    # There is no second tool and no job-detail resource to fetch.
    assert capabilities.supports_description_fetch is False
    assert capabilities.requires_auth is False


def test_the_slug_matches_the_seeded_portal_row() -> None:
    assert ZipRecruiterPortal.slug == "ziprecruiter"


# --- Access: the rule this adapter exists to respect ------------------------


async def test_no_ziprecruiter_web_page_is_ever_requested(stub_mcp, records) -> None:
    """robots.txt closes the whole site to this client.

    'User-agent: * / Allow: /llms.txt / Disallow: /'. Only the published MCP
    endpoint may be called, and it is on a different host.
    """
    calls = stub_mcp(_handler_for([records]))

    await _search(ZipRecruiterPortal())

    assert calls, "the adapter made no request at all"
    for call in calls:
        assert call["url"] == MCP_ENDPOINT
        assert "www.ziprecruiter.com" not in call["url"]


async def test_the_adapter_never_sends_credentials(stub_mcp, records) -> None:
    # The tool is documented as no-auth. Presenting a session or key would be
    # claiming an authorisation this client does not have.
    calls = stub_mcp(_handler_for([records]))

    await _search(ZipRecruiterPortal())

    for call in calls:
        serialised = json.dumps(call["payload"]).lower()
        assert "authorization" not in serialised
        assert "cookie" not in serialised
        assert "password" not in serialised


async def test_url_validation_reports_unverified_rather_than_claiming_a_check(
) -> None:
    # The listing host may not be requested, so no check can happen. Saying
    # True would report a validation that never ran.
    assert await ZipRecruiterPortal().validate_url(
        "https://www.ziprecruiter.com/job-redirect?match_token=abc"
    ) is False


# --- Protocol ---------------------------------------------------------------


async def test_the_handshake_happens_before_any_search(stub_mcp, records) -> None:
    calls = stub_mcp(_handler_for([records]))

    await _search(ZipRecruiterPortal())

    assert calls[0]["payload"]["method"] == "initialize"
    assert calls[1]["payload"]["method"] == "tools/call"
    assert calls[1]["payload"]["params"]["name"] == "search_jobs"


async def test_the_handshake_is_not_repeated_for_every_query(
    stub_mcp, records
) -> None:
    calls = stub_mcp(_handler_for([records]))
    adapter = ZipRecruiterPortal()

    await _search(adapter, queries=("Financial Analyst",))
    await _search(adapter, queries=("FP&A Analyst",))

    handshakes = [c for c in calls if c["payload"]["method"] == "initialize"]
    assert len(handshakes) == 1


async def test_each_query_is_sent_as_its_own_search(stub_mcp, records) -> None:
    calls = stub_mcp(_handler_for([records]))

    await _search(
        ZipRecruiterPortal(),
        queries=("Financial Analyst", "FP&A Analyst"),
        limit=50,
    )

    roles = [
        c["payload"]["params"]["arguments"]["job_role"]
        for c in calls
        if c["payload"]["method"] == "tools/call"
    ]
    assert "Financial Analyst" in roles
    assert "FP&A Analyst" in roles


# --- Freshness windows ------------------------------------------------------


@pytest.mark.parametrize(
    "window",
    [
        FreshnessWindow.LAST_24H,
        FreshnessWindow.LAST_30D,
        FreshnessWindow.OLDER_ACTIVE,
    ],
)
async def test_no_date_filter_is_sent_whatever_the_window(
    stub_mcp, records, window: FreshnessWindow
) -> None:
    """One unfiltered collection, filtered locally afterwards.

    The tool accepts ``max_posted_minutes_ago``, but sending it means the
    collector re-fetches for each of the six windows. That was twelve requests
    per search for the ten records this endpoint surfaces, and it earned a 429
    in live testing.
    """
    calls = stub_mcp(_handler_for([records]))

    await _search(ZipRecruiterPortal(), window=window)

    for call in calls:
        if call["payload"]["method"] == "tools/call":
            arguments = call["payload"]["params"]["arguments"]
            assert "max_posted_minutes_ago" not in arguments


async def test_the_caller_is_told_to_apply_the_window_itself(
    stub_mcp, records
) -> None:
    # Nothing was filtered portal-side, so the collector must filter locally.
    stub_mcp(_handler_for([records]))

    result = await _search(ZipRecruiterPortal())

    assert result.window_applied_by_portal is False
    assert ZipRecruiterPortal().capabilities.supports_date_filter is False


async def test_a_whole_search_costs_at_most_two_calls_per_query(
    stub_mcp, captured
) -> None:
    # The request budget is the point of collecting unfiltered: this portal
    # rate-limits, and a chatty adapter gets the whole portal blocked.
    first = captured["Financial Analyst"][0]["results"]
    second = captured["Financial Analyst"][1]["results"]
    calls = stub_mcp(_handler_for([first, second]))

    await _search(ZipRecruiterPortal(), limit=50)

    searches = [c for c in calls if c["payload"]["method"] == "tools/call"]
    assert len(searches) <= 2


# --- Parsing ----------------------------------------------------------------


async def test_listings_are_mapped_onto_the_shared_contract(
    stub_mcp, records
) -> None:
    stub_mcp(_handler_for([records]))

    result = await _search(ZipRecruiterPortal())

    assert len(result.jobs) == len(records)
    job = result.jobs[0]
    assert job.source == "ziprecruiter"
    assert job.title == records[0]["title"]
    assert job.company == records[0]["company"]
    assert job.location == records[0]["location"]
    assert job.source_url.startswith("https://")
    assert job.application_url == job.source_url


async def test_the_employment_type_is_taken_from_the_job_type(
    stub_mcp, records
) -> None:
    stub_mcp(_handler_for([records]))

    result = await _search(ZipRecruiterPortal())

    by_title = {job.title: job for job in result.jobs}
    for record in records:
        expected = record["job_type"].split("·")[0].strip()
        assert by_title[record["title"]].employment_type == expected


@pytest.mark.parametrize(
    ("job_type", "expected"),
    [
        ("Full-time · On-site", ("Full-time", "On-site")),
        ("Contract · Remote", ("Contract", "Remote")),
        ("Full-time · Remote optional", ("Full-time", "Remote optional")),
        ("Full-time", ("Full-time", None)),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_job_type_splits_into_commitment_and_arrangement(
    job_type: str | None, expected: tuple
) -> None:
    assert ZipRecruiterPortal._job_type_parts(job_type) == expected


async def test_the_work_arrangement_is_preserved_even_though_it_has_no_field(
    stub_mcp, records
) -> None:
    """RawJob has no work-mode field, so it stays in the payload.

    Writing it into ``location`` instead would change the location key that
    cross-portal deduplication groups on, which is a worse trade than losing
    the field.
    """
    stub_mcp(_handler_for([records]))

    result = await _search(ZipRecruiterPortal())

    assert all("job_type" in job.raw_payload for job in result.jobs)
    assert all("is_remote" in job.raw_payload for job in result.jobs)
    assert all(job.location == job.raw_payload["location"] for job in result.jobs)


async def test_the_posted_date_comes_from_the_portals_day_count(
    stub_mcp, records
) -> None:
    stub_mcp(_handler_for([records]))
    before = datetime.now(UTC)

    result = await _search(ZipRecruiterPortal())

    for job, record in zip(result.jobs, records, strict=True):
        assert job.posted_at is not None
        age = before - job.posted_at
        # Day-accurate and no better: the portal publishes whole days only.
        assert abs(age - timedelta(days=record["days_ago"])) < timedelta(minutes=5)


@pytest.mark.parametrize("value", [None, "", "3", -1, True, False])
def test_an_unusable_day_count_leaves_the_date_empty(value: Any) -> None:
    # Better an empty date than a made-up one: freshness ranking reads this.
    assert ZipRecruiterPortal._posted_at(value, now=datetime.now(UTC)) is None


async def test_salary_and_benefits_survive_in_the_payload(
    stub_mcp, records
) -> None:
    # RawJob has no salary field; a later scoring version can re-read these
    # without another request.
    stub_mcp(_handler_for([records]))

    result = await _search(ZipRecruiterPortal())

    assert result.jobs[0].raw_payload["salary"] == records[0]["salary"]
    assert "benefits" in result.jobs[0].raw_payload


# --- The description that does not exist ------------------------------------


async def test_no_description_is_invented_from_the_metadata(
    stub_mcp, records
) -> None:
    """The permitted surface publishes no posting text.

    Composing one from salary, benefits and employment type would clear the
    matching engine's unscoreable-posting gate with text containing no
    requirements, no responsibilities and no skills. That would not make the
    match real, it would only stop the pipeline from noticing it is not.
    """
    stub_mcp(_handler_for([records]))

    result = await _search(ZipRecruiterPortal())

    assert all(job.description is None for job in result.jobs)


async def test_fetching_a_description_does_not_reach_for_a_second_source(
    stub_mcp, records
) -> None:
    calls = stub_mcp(_handler_for([records]))
    adapter = ZipRecruiterPortal()
    result = await _search(adapter)

    before = len(calls)
    assert await adapter.fetch_description(result.jobs[0]) is None
    assert len(calls) == before


# --- Job identity -----------------------------------------------------------


def _captured_rows(captured) -> list[dict[str, Any]]:
    return [
        record
        for pages in captured.values()
        for page in pages
        for record in page["results"]
    ]


def test_one_vacancy_keeps_one_key_across_two_different_urls(captured) -> None:
    """The token rotates per request; the key inside it does not.

    This pair is real: the same Melitta vacancy came back under both the
    "Financial Analyst" and "Senior Financial Analyst" queries, with a
    different match_token each time. Without this, every search would file the
    same job as a new one.
    """
    rows = _captured_rows(captured)
    melitta = [r for r in rows if r["company"].startswith("Melitta")]

    assert len(melitta) == 2, "fixture no longer contains the duplicate pair"
    first, second = melitta
    assert first["job_redirect_url"] != second["job_redirect_url"]
    assert stable_job_key(first["job_redirect_url"]) == stable_job_key(
        second["job_redirect_url"]
    )


def test_every_captured_listing_yields_a_job_key(captured) -> None:
    rows = _captured_rows(captured)

    keyed = {}
    for record in rows:
        key = stable_job_key(record["job_redirect_url"])
        assert key, f"no key recovered for {record['title']}"
        keyed.setdefault(key, []).append(record)

    # A shared key must mean a shared vacancy, never two different ones.
    for group in keyed.values():
        identities = {
            (r["title"], r["company"], r["location"]) for r in group
        }
        assert len(identities) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ziprecruiter.com/job-redirect",
        "https://www.ziprecruiter.com/job-redirect?match_token=",
        "https://www.ziprecruiter.com/job-redirect?match_token=!!!not-base64!!!",
        "https://www.ziprecruiter.com/job-redirect?match_token=AAAA",
        "",
    ],
)
def test_an_unreadable_token_yields_no_key_rather_than_a_wrong_one(
    url: str,
) -> None:
    # The caller then falls back to the URL: the worst case is a duplicate,
    # never a wrong merge.
    assert stable_job_key(url) is None


async def test_the_same_listing_twice_is_collected_once(
    stub_mcp, records
) -> None:
    stub_mcp(_handler_for([records, records]))

    result = await _search(ZipRecruiterPortal(), limit=50)

    ids = [job.external_id for job in result.jobs]
    assert len(ids) == len(set(ids))


# --- Pagination -------------------------------------------------------------


async def test_a_second_page_is_requested_with_an_offset(
    stub_mcp, captured
) -> None:
    first = captured["Financial Analyst"][0]["results"]
    second = captured["Financial Analyst"][1]["results"]
    calls = stub_mcp(_handler_for([first, second]))

    result = await _search(ZipRecruiterPortal(), limit=50)

    offsets = [
        c["payload"]["params"]["arguments"].get("offset")
        for c in calls
        if c["payload"]["method"] == "tools/call"
    ]
    assert offsets[0] is None
    assert offsets[1] == 5
    assert len(result.jobs) == len(first) + len(second)


async def test_paging_stops_when_a_page_repeats_itself(
    stub_mcp, records
) -> None:
    # The endpoint repeats its last page rather than returning an empty one,
    # so an all-duplicate page is how "no more results" presents.
    calls = stub_mcp(_handler_for([records, records, records]))

    await _search(ZipRecruiterPortal(), limit=50)

    searches = [c for c in calls if c["payload"]["method"] == "tools/call"]
    assert len(searches) == 2


async def test_collection_stops_at_the_requested_limit(
    stub_mcp, captured
) -> None:
    first = captured["Financial Analyst"][0]["results"]
    second = captured["Financial Analyst"][1]["results"]
    stub_mcp(_handler_for([first, second]))

    result = await _search(ZipRecruiterPortal(), limit=3)

    assert len(result.jobs) == 3


# --- Malformed and hostile payloads -----------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"result": {}},
        {"result": {"structuredContent": {}}},
        {"result": {"structuredContent": {"results": "not a list"}}},
        {"result": "not an object"},
        {},
    ],
)
async def test_a_malformed_payload_is_a_portal_error_not_a_crash(
    stub_mcp, body: dict
) -> None:
    def handler(call: int, payload: dict) -> dict:
        if payload["method"] == "initialize":
            return {"result": {"protocolVersion": "2024-11-05"}}
        return body

    stub_mcp(handler)

    with pytest.raises(PortalParseError) as exc_info:
        await _search(ZipRecruiterPortal())

    assert exc_info.value.portal == "ziprecruiter"


async def test_a_jsonrpc_error_is_reported_against_this_portal(stub_mcp) -> None:
    def handler(call: int, payload: dict) -> dict:
        if payload["method"] == "initialize":
            return {"result": {"protocolVersion": "2024-11-05"}}
        return {"error": {"code": -32602, "message": "Invalid params"}}

    stub_mcp(handler)

    with pytest.raises(PortalParseError) as exc_info:
        await _search(ZipRecruiterPortal())

    assert "Invalid params" in exc_info.value.message


async def test_an_empty_result_set_is_not_an_error(stub_mcp) -> None:
    # A role with no matches is a legitimate answer, not a failure.
    stub_mcp(_handler_for([[]]))

    result = await _search(ZipRecruiterPortal())

    assert result.jobs == []


async def test_a_record_without_a_link_is_skipped_not_returned(stub_mcp) -> None:
    # A listing the user cannot open is not a result.
    stub_mcp(
        _handler_for(
            [
                [
                    {"title": "Financial Analyst", "company": "Acme"},
                    {
                        "title": "Financial Analyst",
                        "company": "Acme",
                        "job_redirect_url": "https://www.ziprecruiter.com/x?match_token=AA",
                        "days_ago": 1,
                    },
                ]
            ]
        )
    )

    result = await _search(ZipRecruiterPortal())

    assert len(result.jobs) == 1


async def test_a_record_without_a_title_is_skipped(stub_mcp) -> None:
    stub_mcp(
        _handler_for(
            [
                [
                    {
                        "title": "  ",
                        "job_redirect_url": "https://www.ziprecruiter.com/x?match_token=AA",
                    }
                ]
            ]
        )
    )

    result = await _search(ZipRecruiterPortal())

    assert result.jobs == []


async def test_a_non_dict_record_cannot_break_the_batch(stub_mcp) -> None:
    stub_mcp(_handler_for([["not a record", None, 42]]))

    result = await _search(ZipRecruiterPortal())

    assert result.jobs == []


async def test_warnings_from_the_portal_do_not_stop_collection(
    stub_mcp, records
) -> None:
    def handler(call: int, payload: dict) -> dict:
        if payload["method"] == "initialize":
            return {"result": {"protocolVersion": "2024-11-05"}}
        return _envelope(records, warnings=["No location was provided."])

    stub_mcp(handler)

    result = await _search(ZipRecruiterPortal())

    assert len(result.jobs) == len(records)


# --- Transport failures -----------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        PortalTimeoutError("timed out", portal="ziprecruiter"),
        PortalRateLimitedError("slow down", portal="ziprecruiter"),
        PortalBlockedError("refused", portal="ziprecruiter"),
        PortalError("network", portal="ziprecruiter"),
    ],
)
async def test_a_transport_failure_is_raised_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, error: PortalError
) -> None:
    """An empty result would read as "this portal has no matching jobs".

    That is a materially different and misleading claim, so the failure is
    raised and the run records it against this portal alone.
    """

    async def _post_json(self, url, *, payload, headers=None):  # noqa: ANN001
        raise error

    monkeypatch.setattr("app.portals.http.PortalHttpClient.post_json", _post_json)

    with pytest.raises(type(error)):
        await _search(ZipRecruiterPortal())


async def test_the_configured_timeout_and_retries_are_honoured() -> None:
    # Operational policy comes from the portal_configs row, not from code.
    adapter = ZipRecruiterPortal(
        {"timeout_seconds": 9.5, "max_retries": 4, "rate_limit_per_minute": 12}
    )

    assert adapter._http.policy.timeout_seconds == 9.5
    assert adapter._http.policy.max_retries == 4
    assert adapter._http.policy.rate_limit_per_minute == 12


async def test_an_unknown_option_does_not_break_construction() -> None:
    adapter = ZipRecruiterPortal({"some_future_option": True})

    assert adapter.capabilities.requires_auth is False


async def test_a_health_check_reports_failure_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _post_json(self, url, *, payload, headers=None):  # noqa: ANN001
        raise PortalTimeoutError("timed out", portal="ziprecruiter")

    monkeypatch.setattr("app.portals.http.PortalHttpClient.post_json", _post_json)

    health = await ZipRecruiterPortal().health_check()

    assert health.is_available is False
    assert health.detail is not None


async def test_a_health_check_passes_when_the_endpoint_answers(
    stub_mcp,
) -> None:
    stub_mcp(_handler_for([[]]))

    health = await ZipRecruiterPortal().health_check()

    assert health.is_available is True
