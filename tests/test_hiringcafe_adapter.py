"""Hiring Cafe adapter: mapping, parsing and failure handling.

Parsing is tested against a fixture captured from the real, robots-allowed
taxonomy page, so a change to Hiring Cafe's payload shape shows up here rather
than in production. No test in this file touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.constants import FreshnessWindow, JobUrlKind
from app.portals.adapters.hiringcafe import (
    BASE_URL,
    HiringCafePortal,
    slugify_role,
)
from app.portals.base import PortalSearchRequest
from app.portals.exceptions import (
    PortalBlockedError,
    PortalParseError,
    PortalTimeoutError,
)
from app.processing.urls import classify_url

FIXTURE = (
    Path(__file__).parent / "fixtures" / "portals" / "hiringcafe_financial_analyst.json"
)


@pytest.fixture
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def records(payload: dict) -> list[dict]:
    return payload["props"]["pageProps"]["ssrHits"]


@pytest.fixture
def adapter() -> HiringCafePortal:
    return HiringCafePortal(options={"rate_limit_per_minute": 0})


def page_html(payload: dict) -> str:
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></body></html>"
    )


# --- Slug matching ----------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("Financial Analyst", "financial-analyst"),
        ("FP&A Analyst", "fp-a-analyst"),
        ("  Senior   Data Analyst  ", "senior-data-analyst"),
        ("C++ Developer", "c-developer"),
        ("", ""),
    ],
)
def test_roles_become_taxonomy_slugs(role: str, expected: str) -> None:
    assert slugify_role(role) == expected


def test_an_exact_slug_is_preferred() -> None:
    taxonomy = frozenset({"financial-analyst", "analyst"})

    assert (
        HiringCafePortal._match_slug("Financial Analyst", taxonomy)
        == "financial-analyst"
    )


def test_a_seniority_prefixed_title_falls_back_to_the_base_role() -> None:
    taxonomy = frozenset({"financial-analyst"})

    assert (
        HiringCafePortal._match_slug("Senior Financial Analyst", taxonomy)
        == "financial-analyst"
    )


def test_an_unknown_role_matches_nothing_rather_than_guessing() -> None:
    # A wrong slug would mean searching a different role, which the product
    # forbids outright (CLAUDE.md section 3).
    taxonomy = frozenset({"financial-analyst", "accountant"})

    assert HiringCafePortal._match_slug("Marine Biologist", taxonomy) is None
    assert HiringCafePortal._match_slug("", taxonomy) is None


# --- Record mapping ---------------------------------------------------------


def test_records_map_onto_the_raw_job_contract(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    raw = adapter._to_raw_job(records[0], slug="financial-analyst")

    assert raw is not None
    assert raw.source == "hiringcafe"
    assert raw.external_id
    assert raw.title
    assert raw.company
    assert raw.location
    assert raw.description
    assert raw.source_url == f"{BASE_URL}/jobs/financial-analyst"
    assert raw.application_url
    assert raw.posted_at is not None
    assert raw.employment_type


def test_the_source_url_is_the_robots_allowed_taxonomy_page(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    # Never a ?searchState= or ?page= URL: both are disallowed by robots.txt.
    raw = adapter._to_raw_job(records[0], slug="financial-analyst")

    assert raw is not None
    assert raw.source_url.startswith(f"{BASE_URL}/jobs/")
    assert "searchState" not in raw.source_url
    assert "page=" not in raw.source_url
    assert "/viewjob/" not in raw.source_url


def test_the_application_url_points_at_the_employer_not_the_board(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    # This is what makes Hiring Cafe valuable next to a board: the user lands
    # on the employer's own application system.
    raw = adapter._to_raw_job(records[0], slug="financial-analyst")

    assert raw is not None
    assert raw.application_url is not None
    assert "hiringcafe.com" not in raw.application_url

    kind = classify_url(raw.application_url, company=raw.company)
    assert kind in (JobUrlKind.ATS, JobUrlKind.EMPLOYER_CAREER)


def test_posted_at_is_parsed_as_timezone_aware_utc(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    raw = adapter._to_raw_job(records[0], slug="financial-analyst")

    assert raw is not None
    assert raw.posted_at is not None
    assert raw.posted_at.tzinfo is not None
    assert raw.posted_at.utcoffset() == datetime.now(UTC).utcoffset()


def test_the_description_is_composed_from_the_structured_fields(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    # Hiring Cafe publishes no posting body on this surface, so the adapter
    # assembles the fields it does publish. The requirements must survive:
    # they are exactly what the mandatory-skill checks read later.
    raw = adapter._to_raw_job(records[0], slug="financial-analyst")

    assert raw is not None
    assert "Requirements:" in raw.description
    assert len(raw.description) >= 80


def test_a_description_with_nothing_in_it_is_none(
    adapter: HiringCafePortal,
) -> None:
    # A stub would look like a real posting to everything downstream.
    assert adapter._description({}) is None


def test_an_expired_listing_is_skipped(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    expired = dict(records[0])
    expired["is_expired"] = True

    assert adapter._to_raw_job(expired, slug="financial-analyst") is None


def test_a_record_missing_an_essential_field_is_skipped(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    for field in ("id", "objectID", "apply_url"):
        broken = dict(records[0])
        broken.pop(field, None)
        if field == "id":
            broken.pop("objectID", None)
        if field == "apply_url":
            broken["apply_url"] = ""
            assert adapter._to_raw_job(broken, slug="x") is None

    untitled = dict(records[0])
    untitled["job_information"] = {}
    assert adapter._to_raw_job(untitled, slug="x") is None


def test_a_record_with_no_optional_data_still_maps(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    minimal = {
        "id": "abc123",
        "apply_url": "https://employer.invalid/jobs/1",
        "job_information": {"title": "Financial Analyst"},
        "v5_processed_job_data": {},
        "enriched_company_data": {},
    }

    raw = adapter._to_raw_job(minimal, slug="financial-analyst")

    assert raw is not None
    assert raw.company is None
    assert raw.location is None
    assert raw.description is None
    assert raw.posted_at is None
    assert raw.employment_type is None


def test_an_unparseable_publish_date_does_not_break_the_record(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    broken = dict(records[0])
    broken["v5_processed_job_data"] = dict(broken["v5_processed_job_data"])
    broken["v5_processed_job_data"]["estimated_publish_date"] = "not a date"

    raw = adapter._to_raw_job(broken, slug="financial-analyst")

    assert raw is not None
    assert raw.posted_at is None


def test_a_missing_posted_date_is_not_invented(adapter: HiringCafePortal) -> None:
    assert adapter._posted_at(None) is None
    assert adapter._posted_at("") is None


# --- Privacy ----------------------------------------------------------------


def test_other_users_identifiers_are_never_stored(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    # `raw_payload` is persisted. These arrays are other people's account ids.
    polluted = dict(records[0])
    polluted["job_information"] = {
        **polluted["job_information"],
        "viewedByUsers": ["someone-else-1", "someone-else-2"],
        "appliedFromUsers": ["someone-else-3"],
    }

    raw = adapter._to_raw_job(polluted, slug="financial-analyst")

    assert raw is not None
    assert "viewedByUsers" not in raw.raw_payload["job_information"]
    assert "appliedFromUsers" not in raw.raw_payload["job_information"]
    assert "someone-else-1" not in json.dumps(raw.raw_payload)


def test_stripping_identifiers_leaves_the_rest_of_the_record(
    adapter: HiringCafePortal, records: list[dict]
) -> None:
    raw = adapter._to_raw_job(records[0], slug="financial-analyst")

    assert raw is not None
    assert raw.raw_payload["job_information"]["title"]
    assert raw.raw_payload["apply_url"]


# --- Field extraction -------------------------------------------------------


def test_the_workplace_type_is_carried_in_the_description(
    adapter: HiringCafePortal,
) -> None:
    composed = adapter._description(
        {
            "requirements_summary": "A" * 100,
            "workplace_type": "Remote",
        }
    )

    assert "Workplace type: Remote" in composed


def test_location_falls_back_when_there_is_no_formatted_string(
    adapter: HiringCafePortal,
) -> None:
    assert adapter._location({"workplace_cities": ["Austin, Texas, US"]}) == (
        "Austin, Texas, US"
    )
    assert adapter._location({"workplace_countries": ["US"]}) == "US"
    assert adapter._location({}) is None


def test_a_degree_is_only_claimed_when_it_is_required(
    adapter: HiringCafePortal,
) -> None:
    # "Not Mentioned" is not a requirement; claiming one would invent a hard
    # reject the posting never made.
    assert adapter._education_line({"bachelors_degree_requirement": "Not Mentioned"}) is None
    assert adapter._education_line({"bachelors_degree_requirement": "Required"})


def test_experience_is_only_stated_when_the_posting_gave_a_number(
    adapter: HiringCafePortal,
) -> None:
    assert adapter._experience_line(
        {"is_min_industry_and_role_yoe_not_mentioned": True, "min_industry_and_role_yoe": 5}
    ) is None
    assert adapter._experience_line({"min_industry_and_role_yoe": 0}) is None
    assert "5+ years" in adapter._experience_line({"min_industry_and_role_yoe": 5})


def test_the_employment_type_reads_the_commitment(
    adapter: HiringCafePortal,
) -> None:
    assert adapter._employment_type({"commitment": ["Full Time"]}) == "Full Time"
    assert adapter._employment_type({"commitment": "Part Time"}) == "Part Time"
    assert adapter._employment_type({}) is None


# --- Page parsing -----------------------------------------------------------


def test_a_page_without_embedded_data_is_a_parse_error(
    adapter: HiringCafePortal,
) -> None:
    with pytest.raises(PortalParseError):
        adapter._extract_records("<html><body>nothing here</body></html>")


def test_malformed_embedded_json_is_a_parse_error(adapter: HiringCafePortal) -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">{not json}</script>'
    )

    with pytest.raises(PortalParseError):
        adapter._extract_records(html)


def test_a_missing_hits_array_is_a_parse_error(adapter: HiringCafePortal) -> None:
    # A shape change must be surfaced, not silently read as "no jobs".
    html = page_html({"props": {"pageProps": {}}})

    with pytest.raises(PortalParseError):
        adapter._extract_records(html)


def test_an_empty_hits_array_is_not_an_error(adapter: HiringCafePortal) -> None:
    html = page_html({"props": {"pageProps": {"ssrHits": []}}})

    assert adapter._extract_records(html) == []


def test_non_dict_entries_are_ignored(adapter: HiringCafePortal) -> None:
    html = page_html({"props": {"pageProps": {"ssrHits": [None, "x", {"id": "1"}]}}})

    assert adapter._extract_records(html) == [{"id": "1"}]


# --- Search -----------------------------------------------------------------


async def test_search_collects_and_respects_the_limit(
    adapter: HiringCafePortal, payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_taxonomy(self):  # noqa: ANN001
        return frozenset({"financial-analyst"})

    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        return page_html(payload)

    monkeypatch.setattr(HiringCafePortal, "_load_taxonomy", fake_taxonomy)
    monkeypatch.setattr(
        "app.portals.http.PortalHttpClient.get_text", fake_get_text
    )

    result = await adapter.search(
        PortalSearchRequest(
            queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=3
        )
    )

    assert len(result.jobs) == 3
    assert result.has_more is False
    assert result.window_applied_by_portal is False


async def test_search_deduplicates_across_queries(
    adapter: HiringCafePortal, payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_taxonomy(self):  # noqa: ANN001
        return frozenset({"financial-analyst", "fpa-analyst"})

    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        calls.append(url)
        return page_html(payload)

    monkeypatch.setattr(HiringCafePortal, "_load_taxonomy", fake_taxonomy)
    monkeypatch.setattr(
        "app.portals.http.PortalHttpClient.get_text", fake_get_text
    )

    result = await adapter.search(
        PortalSearchRequest(
            queries=("Financial Analyst", "FPA Analyst"),
            window=FreshnessWindow.LAST_7D,
            limit=50,
        )
    )

    ids = [job.external_id for job in result.jobs]
    assert len(ids) == len(set(ids))
    assert len(calls) == 2


async def test_a_query_with_no_taxonomy_match_is_skipped_quietly(
    adapter: HiringCafePortal, payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_taxonomy(self):  # noqa: ANN001
        return frozenset({"financial-analyst"})

    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        calls.append(url)
        return page_html(payload)

    monkeypatch.setattr(HiringCafePortal, "_load_taxonomy", fake_taxonomy)
    monkeypatch.setattr(
        "app.portals.http.PortalHttpClient.get_text", fake_get_text
    )

    result = await adapter.search(
        PortalSearchRequest(
            queries=("Marine Biologist", "Financial Analyst"),
            window=FreshnessWindow.LAST_7D,
            limit=50,
        )
    )

    assert len(calls) == 1
    assert result.jobs


async def test_the_adapter_only_requests_allowed_paths(
    adapter: HiringCafePortal, payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # robots.txt disallows the search surface, pagination, /viewjob/, /org/,
    # /company/ and /b/. Nothing here may request any of them.
    requested: list[str] = []

    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        requested.append(url)
        if url.endswith(".xml"):
            return "<urlset><url><loc>https://hiringcafe.com/jobs/financial-analyst</loc></url></urlset>"
        return page_html(payload)

    monkeypatch.setattr(
        "app.portals.http.PortalHttpClient.get_text", fake_get_text
    )
    HiringCafePortal._taxonomy_cache = None

    await adapter.search(
        PortalSearchRequest(
            queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=5
        )
    )
    HiringCafePortal._taxonomy_cache = None

    assert requested
    for url in requested:
        assert "searchState" not in url, url
        assert "page=" not in url, url
        for forbidden in ("/viewjob/", "/org/", "/company/", "/b/", "/req/", "/board/"):
            assert forbidden not in url, (forbidden, url)


async def test_an_empty_taxonomy_sitemap_is_a_parse_error(
    adapter: HiringCafePortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        return "<urlset></urlset>"

    monkeypatch.setattr(
        "app.portals.http.PortalHttpClient.get_text", fake_get_text
    )
    HiringCafePortal._taxonomy_cache = None

    with pytest.raises(PortalParseError):
        await adapter._load_taxonomy()

    HiringCafePortal._taxonomy_cache = None


async def test_a_transport_failure_propagates_as_a_portal_error(
    adapter: HiringCafePortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The orchestrator records it against this portal's leg and carries on;
    # returning an empty result would read as "no matching jobs".
    async def fake_taxonomy(self):  # noqa: ANN001
        return frozenset({"financial-analyst"})

    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        raise PortalTimeoutError("timed out", portal="hiringcafe")

    monkeypatch.setattr(HiringCafePortal, "_load_taxonomy", fake_taxonomy)
    monkeypatch.setattr(
        "app.portals.http.PortalHttpClient.get_text", fake_get_text
    )

    with pytest.raises(PortalTimeoutError):
        await adapter.search(
            PortalSearchRequest(
                queries=("Financial Analyst",),
                window=FreshnessWindow.LAST_7D,
                limit=5,
            )
        )


# --- Bot challenges: reported as a refusal, never satisfied -----------------

#: What hiringcafe.com actually served on 2026-08-25, trimmed. The site is
#: fronted by a JavaScript bot challenge on every path - `/robots.txt`
#: included - which is why this adapter collects nothing at present.
CHALLENGE_HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8"><title>Vercel Security Checkpoint</title></head>
<body><h1>Vercel Security Checkpoint</h1>
<p>We're verifying your browser</p>
<noscript>Enable JavaScript to continue</noscript>
</body></html>"""


def test_a_challenge_page_is_a_refusal_not_a_format_change(
    adapter: HiringCafePortal,
) -> None:
    """The distinction matters to whoever reads the error.

    The challenge normally arrives as 403 or 429, which the shared HTTP client
    already classifies. But an interstitial can be served with 200, and then it
    reaches the parser - which finds no `__NEXT_DATA__` and, before this,
    reported that Hiring Cafe's page format had changed. That would send
    somebody to rewrite a parser that is working perfectly well.
    """
    with pytest.raises(PortalBlockedError) as exc_info:
        adapter._extract_records(CHALLENGE_HTML)

    assert "challenge" in exc_info.value.message.lower()
    # And it says what we are *not* doing about it.
    assert "rather than attempting to satisfy" in exc_info.value.message


def test_a_genuinely_broken_page_is_still_a_parse_error(
    adapter: HiringCafePortal,
) -> None:
    """The challenge check must not swallow real format changes."""
    with pytest.raises(PortalParseError):
        adapter._extract_records("<html><body><p>Some other page</p></body></html>")


def test_a_challenge_is_not_mistaken_for_a_listing_page(
    adapter: HiringCafePortal, payload: dict
) -> None:
    """A real page mentioning those words in a posting still parses.

    The markers are only looked for at the very top of the document, so a job
    description that happens to discuss browser verification cannot make a
    working page look like a block.
    """
    html = page_html(payload).replace(
        "<html>", "<html><!-- just a moment... -->", 1
    )
    assert adapter._extract_records(html), "a real page must still parse"


async def test_a_challenge_during_search_does_not_become_zero_jobs(
    adapter: HiringCafePortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour that matters: a blocked portal is not an empty portal."""

    async def fake_taxonomy(self):  # noqa: ANN001
        return frozenset({"financial-analyst"})

    async def challenged(self, url, **kwargs):  # noqa: ANN001
        return CHALLENGE_HTML

    monkeypatch.setattr(HiringCafePortal, "_load_taxonomy", fake_taxonomy)
    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", challenged)

    with pytest.raises(PortalBlockedError):
        await adapter.search(
            PortalSearchRequest(
                queries=("Financial Analyst",),
                window=FreshnessWindow.LAST_7D,
                limit=5,
            )
        )


async def test_a_rate_limited_taxonomy_load_is_not_zero_jobs(
    adapter: HiringCafePortal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """429 on the sitemap ends the pass loudly, not with an empty result."""
    from app.portals.exceptions import PortalRateLimitedError

    async def limited(self, url, **kwargs):  # noqa: ANN001
        raise PortalRateLimitedError("slow down", portal="hiringcafe")

    monkeypatch.setattr(HiringCafePortal, "_taxonomy_cache", None)
    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", limited)

    with pytest.raises(PortalRateLimitedError):
        await adapter.search(
            PortalSearchRequest(
                queries=("Financial Analyst",),
                window=FreshnessWindow.LAST_7D,
                limit=5,
            )
        )


async def test_fetch_description_makes_no_second_request(
    adapter: HiringCafePortal, records: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(self, url, **kwargs):  # noqa: ANN001
        raise AssertionError(f"unexpected request to {url}")

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", explode)

    raw = adapter._to_raw_job(records[0], slug="financial-analyst")
    assert raw is not None

    assert await adapter.fetch_description(raw) == raw.description


# --- Policy -----------------------------------------------------------------


def test_the_adapter_declares_what_the_allowed_surface_can_do(
    adapter: HiringCafePortal,
) -> None:
    capabilities = adapter.capabilities

    # No date filter and no paging on the allowed surface, so the planner
    # applies freshness locally and does not ask for page 2.
    assert capabilities.supports_date_filter is False
    assert capabilities.supports_pagination is False
    assert capabilities.requires_auth is False
    assert capabilities.provides_application_url is True


def test_the_adapter_uses_the_configured_policy() -> None:
    portal = HiringCafePortal(
        options={"timeout_seconds": 12.5, "max_retries": 4, "rate_limit_per_minute": 5}
    )

    assert portal._http.policy.timeout_seconds == 12.5
    assert portal._http.policy.max_retries == 4
    assert portal._http.policy.rate_limit_per_minute == 5


def test_no_auto_apply_concept_exists_in_the_adapter() -> None:
    source = (
        Path(__file__).parent.parent
        / "app"
        / "portals"
        / "adapters"
        / "hiringcafe.py"
    ).read_text(encoding="utf-8")
    collapsed = source.lower().replace(" ", "").replace("_", "")

    for banned in ("easyapply", "autoapply", "clickapply", "submitapplication"):
        assert banned not in collapsed, banned
