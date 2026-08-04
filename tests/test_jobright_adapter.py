"""Jobright adapter: mapping, parsing and failure handling.

Parsing is tested against a fixture captured from the real, robots-allowed
page, so a change to Jobright's payload shape shows up here rather than in
production. No test in this file touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.constants import FreshnessWindow
from app.portals.adapters.jobright import JobrightPortal, slugify_role
from app.portals.base import PortalSearchRequest
from app.portals.exceptions import PortalParseError

FIXTURE = Path(__file__).parent / "fixtures" / "portals" / "jobright_financial_analyst.json"
TAXONOMY = frozenset({"financial-analyst", "backend-engineer", "data-analyst", "finance"})


@pytest.fixture
def fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def fixture_html(fixture_payload: dict) -> str:
    """Wrap the captured data the way the real page embeds it."""
    inner = json.dumps({"props": fixture_payload["props"]})
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{inner}</script></body></html>"
    )


@pytest.fixture
def adapter() -> JobrightPortal:
    portal = JobrightPortal(options={})
    # Pin the taxonomy so tests never depend on a live sitemap fetch.
    type(portal)._taxonomy_cache = TAXONOMY
    return portal


# --- Slug matching ----------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("Financial Analyst", "financial-analyst"),
        ("financial analyst", "financial-analyst"),
        ("  Financial   Analyst  ", "financial-analyst"),
        ("FP&A Analyst", "fp-a-analyst"),
    ],
)
def test_roles_become_taxonomy_slugs(role: str, expected: str) -> None:
    assert slugify_role(role) == expected


def test_a_seniority_prefixed_title_falls_back_to_the_base_role(
    adapter: JobrightPortal,
) -> None:
    # "Senior Financial Analyst" is the same role, so it must still search it.
    assert adapter._match_slug("Senior Financial Analyst", TAXONOMY) == (
        "financial-analyst"
    )


def test_an_unknown_role_matches_nothing_rather_than_guessing(
    adapter: JobrightPortal,
) -> None:
    # Returning a near-miss slug would mean searching a different role, which
    # the product forbids outright.
    assert adapter._match_slug("Underwater Basket Weaver", TAXONOMY) is None


# --- Mapping ----------------------------------------------------------------


def test_records_map_onto_the_raw_job_contract(
    adapter: JobrightPortal, fixture_payload: dict
) -> None:
    records = fixture_payload["props"]["pageProps"]["jobList"]
    job = adapter._to_raw_job(records[0])

    assert job is not None
    assert job.source == "jobright"
    assert job.external_id
    assert job.title
    assert job.company
    assert job.source_url.startswith("https://jobright.ai/")
    assert job.raw_payload  # the untouched record, kept for later re-extraction


def test_the_application_url_is_usable_for_manual_application(
    adapter: JobrightPortal, fixture_payload: dict
) -> None:
    records = fixture_payload["props"]["pageProps"]["jobList"]

    for record in records:
        job = adapter._to_raw_job(record)
        assert job is not None
        # The user has to be able to open something.
        assert job.application_url
        assert job.application_url.startswith("http")


def test_posted_at_is_parsed_as_timezone_aware_utc(
    adapter: JobrightPortal, fixture_payload: dict
) -> None:
    records = fixture_payload["props"]["pageProps"]["jobList"]
    job = adapter._to_raw_job(records[0])

    assert job is not None
    assert isinstance(job.posted_at, datetime)
    assert job.posted_at.tzinfo is UTC


def test_the_description_keeps_requirements(
    adapter: JobrightPortal, fixture_payload: dict
) -> None:
    # Requirements are exactly what the Phase 6 mandatory-skill checks need;
    # dropping them would quietly weaken matching later.
    records = fixture_payload["props"]["pageProps"]["jobList"]
    described = [
        adapter._to_raw_job(record) for record in records
    ]
    bodies = [job.description for job in described if job and job.description]

    assert bodies
    assert any("Requirements:" in body for body in bodies)


@pytest.mark.parametrize(
    "missing",
    ["jobId", "jobTitle", "url"],
)
def test_a_record_missing_an_essential_field_is_skipped(
    adapter: JobrightPortal, fixture_payload: dict, missing: str
) -> None:
    record = json.loads(json.dumps(fixture_payload["props"]["pageProps"]["jobList"][0]))
    record["jobResult"].pop(missing, None)
    if missing == "url":
        record["jobResult"].pop("applyLink", None)

    assert adapter._to_raw_job(record) is None


def test_optional_fields_absent_become_none_not_empty_strings(
    adapter: JobrightPortal, fixture_payload: dict
) -> None:
    record = json.loads(json.dumps(fixture_payload["props"]["pageProps"]["jobList"][0]))
    record["companyResult"] = {}
    for key in ("jobLocation", "jobLocations", "employmentType", "jobSummary",
                "coreResponsibilities", "requirements", "publishTime"):
        record["jobResult"].pop(key, None)

    job = adapter._to_raw_job(record)

    assert job is not None
    assert job.company is None
    assert job.location is None
    assert job.employment_type is None
    assert job.description is None
    assert job.posted_at is None


def test_an_unparseable_publish_time_does_not_break_the_record(
    adapter: JobrightPortal, fixture_payload: dict
) -> None:
    record = json.loads(json.dumps(fixture_payload["props"]["pageProps"]["jobList"][0]))
    record["jobResult"]["publishTime"] = "not a date"

    job = adapter._to_raw_job(record)

    assert job is not None
    assert job.posted_at is None


# --- Page parsing -----------------------------------------------------------


def test_a_page_without_embedded_data_is_a_parse_error(
    adapter: JobrightPortal,
) -> None:
    with pytest.raises(PortalParseError):
        adapter._extract_records("<html><body>nothing here</body></html>")


def test_malformed_embedded_json_is_a_parse_error(adapter: JobrightPortal) -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">{not json}</script>'
    )

    with pytest.raises(PortalParseError):
        adapter._extract_records(html)


def test_a_missing_job_list_is_a_parse_error(adapter: JobrightPortal) -> None:
    # An empty list is a legitimate "no matches"; a missing key means the page
    # shape changed and should be surfaced rather than read as zero results.
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{}}}</script>'
    )

    with pytest.raises(PortalParseError):
        adapter._extract_records(html)


def test_an_empty_job_list_is_not_an_error(adapter: JobrightPortal) -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"jobList":[]}}}</script>'
    )

    assert adapter._extract_records(html) == []


# --- Search -----------------------------------------------------------------


async def test_search_collects_and_respects_the_limit(
    adapter: JobrightPortal, fixture_html: str
) -> None:
    async def _fetch(*_args, **_kwargs) -> str:
        return fixture_html

    adapter._http.get_text = _fetch  # type: ignore[attr-defined]

    result = await adapter.search(
        PortalSearchRequest(
            queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=2
        )
    )

    assert len(result.jobs) == 2
    assert all(job.source == "jobright" for job in result.jobs)
    # The surface does not page, so the adapter must not claim it can.
    assert result.has_more is False
    assert result.next_cursor is None


async def test_search_deduplicates_across_queries(
    adapter: JobrightPortal, fixture_html: str
) -> None:
    # Two title variations frequently resolve to overlapping taxonomy pages.
    async def _fetch(*_args, **_kwargs) -> str:
        return fixture_html

    adapter._http.get_text = _fetch  # type: ignore[attr-defined]

    result = await adapter.search(
        PortalSearchRequest(
            queries=("Financial Analyst", "Finance"),
            window=FreshnessWindow.LAST_7D,
            limit=10,
        )
    )

    ids = [job.external_id for job in result.jobs]
    assert len(ids) == len(set(ids))


async def test_a_query_with_no_taxonomy_match_is_skipped_quietly(
    adapter: JobrightPortal, fixture_html: str
) -> None:
    calls: list[str] = []

    async def _fetch(url: str, **_kwargs) -> str:
        calls.append(url)
        return fixture_html

    adapter._http.get_text = _fetch  # type: ignore[attr-defined]

    result = await adapter.search(
        PortalSearchRequest(
            queries=("Underwater Basket Weaver", "Financial Analyst"),
            window=FreshnessWindow.LAST_7D,
            limit=5,
        )
    )

    # Only the matched role was fetched; the unmatched one did not error.
    assert len(calls) == 1
    assert "financial-analyst" in calls[0]
    assert result.jobs


async def test_the_adapter_never_calls_the_disallowed_api_surface(
    adapter: JobrightPortal, fixture_html: str
) -> None:
    """robots.txt disallows /api/*. Nothing here may reach for it."""
    calls: list[str] = []

    async def _fetch(url: str, **_kwargs) -> str:
        calls.append(url)
        return fixture_html

    adapter._http.get_text = _fetch  # type: ignore[attr-defined]

    await adapter.search(
        PortalSearchRequest(
            queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=5
        )
    )

    assert calls
    for url in calls:
        assert "/api/" not in url, url


async def test_fetch_description_does_not_make_a_second_request(
    adapter: JobrightPortal, fixture_payload: dict
) -> None:
    # The detail page renders from the disallowed API, so the summary that
    # arrived with the listing is all there is.
    async def _explode(*_args, **_kwargs) -> str:
        raise AssertionError("fetch_description must not hit the network")

    adapter._http.get_text = _explode  # type: ignore[attr-defined]
    job = adapter._to_raw_job(fixture_payload["props"]["pageProps"]["jobList"][0])
    assert job is not None

    assert await adapter.fetch_description(job) == job.description
