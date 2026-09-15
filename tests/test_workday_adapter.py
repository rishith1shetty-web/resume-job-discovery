"""The Workday adapter, against payloads captured from live tenants.

``tests/fixtures/portals/workday_payloads.json`` holds real responses - three
tenants' ``robots.txt``, one list page, and detail payloads covering a United
States posting, a multi-location United States posting, a non-United-States
posting, an explicit Remote and an explicit Hybrid. Parsing is tested against
those rather than against hand-written shapes, so a change in Workday's schema
shows up here as a failure rather than in production as a silent zero.

Transport is stubbed throughout: no test in this file touches the network.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import ApplicationType, FreshnessWindow
from app.portals.adapters.workday import (
    DEFAULT_MAX_CONCURRENT_TENANTS,
    DEFAULT_TENANTS,
    MAX_PAGES_PER_QUERY,
    PAGE_SIZE,
    STALE_POSTED_MARKER,
    WorkdayPortal,
    WorkdayTenant,
    additional_locations,
    build_description,
    country_code,
    employment_type,
    format_location,
    html_to_text,
    is_stale_label,
    is_us_posting,
    parse_start_date,
    relative_label_days,
    title_matches,
    title_tokens,
    workplace_label,
)
from app.portals.base import PortalSearchRequest, RawJob
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.processing.titles import detect_work_mode
from app.processing.urls import JobUrlKind, classify_url
from app.processing.us_locations import classify_us_location
from app.search.freshness import is_eligible_age

FIXTURE = Path(__file__).parent / "fixtures" / "portals" / "workday_payloads.json"
PAYLOADS: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))

ROBOTS: dict[str, str] = PAYLOADS["robots"]
LIST_USBANK: dict[str, Any] = PAYLOADS["list_usbank"]
DETAILS: dict[str, Any] = PAYLOADS["details"]

TENANT = DEFAULT_TENANTS[0]


def detail(name: str) -> dict[str, Any]:
    return DETAILS[name]["detail"]


def posting(name: str) -> dict[str, Any]:
    return detail(name)["jobPostingInfo"]


def row(name: str) -> dict[str, Any]:
    return DETAILS[name]["row"]


# --------------------------------------------------------------------------
# A stub transport
# --------------------------------------------------------------------------


class FakeClient:
    """Stands in for ``PortalHttpClient``, recording every request made.

    The request log is the point: several tests here assert on *how many*
    requests a collection cost, which is the property that stops synonymous
    query variations from multiplying into repeated detail fetches.
    """

    def __init__(
        self,
        *,
        robots: str | None = None,
        robots_error: Exception | None = None,
        lists: dict[tuple[str, int], dict[str, Any]] | None = None,
        details: dict[str, dict[str, Any]] | None = None,
        list_error: Exception | None = None,
        detail_error: Exception | None = None,
    ) -> None:
        self.robots = robots
        self.robots_error = robots_error
        self.lists = lists or {}
        self.details = details or {}
        self.list_error = list_error
        self.detail_error = detail_error
        self.requests: list[str] = []

    async def get_optional_text(self, url: str, **_: Any) -> str | None:
        self.requests.append(f"ROBOTS {url}")
        if self.robots_error is not None:
            raise self.robots_error
        return self.robots

    async def post_json(self, url: str, *, payload: dict, **_: Any) -> dict:
        self.requests.append(f"LIST {url} {payload['searchText']}@{payload['offset']}")
        if self.list_error is not None:
            raise self.list_error
        key = (payload["searchText"], payload["offset"])
        return self.lists.get(key, {"total": 0, "jobPostings": []})

    async def get_json(self, url: str, **_: Any) -> dict:
        self.requests.append(f"DETAIL {url}")
        if self.detail_error is not None:
            raise self.detail_error
        if url not in self.details:
            raise PortalError("not found", portal="workday")
        return self.details[url]

    async def head_ok(self, url: str) -> bool:
        self.requests.append(f"HEAD {url}")
        return True

    @property
    def list_calls(self) -> list[str]:
        return [r for r in self.requests if r.startswith("LIST ")]

    @property
    def detail_calls(self) -> list[str]:
        return [r for r in self.requests if r.startswith("DETAIL ")]

    @property
    def robots_calls(self) -> list[str]:
        return [r for r in self.requests if r.startswith("ROBOTS ")]


ALLOW_ALL = "User-agent: *\nDisallow: /refreshFacet/\n"


def build_portal(
    tenants: list[WorkdayTenant] | None = None,
    *,
    clients: dict[str, FakeClient] | None = None,
    robots: str | None = ALLOW_ALL,
    lists: dict[tuple[str, int], dict[str, Any]] | None = None,
    details: dict[str, dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    **client_kwargs: Any,
) -> tuple[WorkdayPortal, dict[str, FakeClient]]:
    """A portal whose every host is served by a recording stub."""
    chosen = tenants if tenants is not None else [TENANT]
    config = dict(options or {})
    config.setdefault("tenants", [t.as_dict() for t in chosen])

    portal = WorkdayPortal(config)
    made: dict[str, FakeClient] = dict(clients or {})

    def client_for(tenant: WorkdayTenant) -> FakeClient:
        if tenant.host not in made:
            made[tenant.host] = FakeClient(
                robots=robots, lists=lists, details=details, **client_kwargs
            )
        return made[tenant.host]

    portal._client_for = client_for  # type: ignore[method-assign]
    return portal, made


def request_for(*queries: str, limit: int = 100) -> PortalSearchRequest:
    return PortalSearchRequest(
        queries=queries, window=FreshnessWindow.LAST_30D, limit=limit
    )


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Registration, capabilities, configuration
# --------------------------------------------------------------------------


def test_the_adapter_is_registered_under_its_slug() -> None:
    from app.portals.registry import registry

    import app.portals.adapters  # noqa: F401  - registers on import

    assert registry.is_registered("workday")
    assert registry.get("workday") is WorkdayPortal


def test_slug_and_display_name() -> None:
    assert WorkdayPortal.slug == "workday"
    assert WorkdayPortal.display_name == "Workday"


def test_capabilities_state_what_workday_actually_does() -> None:
    capabilities = WorkdayPortal().capabilities

    # No tenant publishes a date facet, so the caller filters.
    assert capabilities.supports_date_filter is False
    # `search` already returns the full text from the detail it opens.
    assert capabilities.supports_description_fetch is False
    assert capabilities.supports_pagination is True
    assert capabilities.requires_auth is False
    assert capabilities.provides_application_url is True


def test_an_unconfigured_portal_reads_the_verified_defaults() -> None:
    assert WorkdayPortal().tenants == DEFAULT_TENANTS


def test_configuration_replaces_the_defaults_entirely() -> None:
    """Adding an employer must never require a deployment."""
    portal = WorkdayPortal(
        {"tenants": [{"tenant": "acme", "wd": "5", "site": "Careers", "display_name": "Acme"}]}
    )

    assert [t.as_dict() for t in portal.tenants] == [
        {"tenant": "acme", "wd": "5", "site": "Careers", "display_name": "Acme"}
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"wd": "5", "site": "Careers", "display_name": "Acme"},
        {"tenant": "acme", "site": "Careers", "display_name": "Acme"},
        {"tenant": "acme", "wd": "5", "display_name": "Acme"},
        {"tenant": "acme", "wd": "5", "site": "Careers"},
        {"tenant": "acme", "wd": "5", "site": "Careers", "display_name": "  "},
        {"tenant": "acme", "wd": "five", "site": "Careers", "display_name": "Acme"},
    ],
)
def test_an_incomplete_tenant_entry_is_dropped_not_half_built(entry: dict) -> None:
    """A tenant with no display_name would publish under a blank employer."""
    portal = WorkdayPortal({"tenants": [entry]})

    # Nothing usable was configured, so the verified defaults stand.
    assert portal.tenants == DEFAULT_TENANTS


def test_a_wd_value_copied_out_of_a_url_still_works() -> None:
    portal = WorkdayPortal(
        {"tenants": [{"tenant": "acme", "wd": "wd12", "site": "S", "display_name": "A"}]}
    )

    assert portal.tenants[0].wd == "12"
    assert portal.tenants[0].host == "https://acme.wd12.myworkdayjobs.com"


def test_concurrency_is_configurable_and_never_zero() -> None:
    assert WorkdayPortal()._max_concurrent == DEFAULT_MAX_CONCURRENT_TENANTS
    assert WorkdayPortal({"max_concurrent_tenants": 3})._max_concurrent == 3
    assert WorkdayPortal({"max_concurrent_tenants": 0})._max_concurrent == 1
    assert WorkdayPortal({"max_concurrent_tenants": "many"})._max_concurrent == (
        DEFAULT_MAX_CONCURRENT_TENANTS
    )


# --------------------------------------------------------------------------
# URL construction
# --------------------------------------------------------------------------


def test_tenant_urls_are_built_from_the_four_configured_facts() -> None:
    tenant = WorkdayTenant("acme", "12", "Acme_Careers", "Acme Corp")

    assert tenant.host == "https://acme.wd12.myworkdayjobs.com"
    assert tenant.site_url == "https://acme.wd12.myworkdayjobs.com/Acme_Careers/"
    assert tenant.jobs_url == (
        "https://acme.wd12.myworkdayjobs.com/wday/cxs/acme/Acme_Careers/jobs"
    )
    assert tenant.detail_url("/job/X/Analyst_R1") == (
        "https://acme.wd12.myworkdayjobs.com/wday/cxs/acme/Acme_Careers/job/X/Analyst_R1"
    )


def test_the_wd_number_lands_in_the_host_not_the_path() -> None:
    """A tenant on wd12 reached on wd1 is a different company's board."""
    assert WorkdayTenant("a", "1", "S", "A").host.startswith("https://a.wd1.")
    assert WorkdayTenant("a", "12", "S", "A").host.startswith("https://a.wd12.")


def test_the_site_appears_in_the_cxs_path_and_the_site_url() -> None:
    tenant = WorkdayTenant("acme", "5", "External_Career_Site", "Acme")

    assert "/External_Career_Site/jobs" in tenant.jobs_url
    assert tenant.site_url.endswith("/External_Career_Site/")


def test_two_sites_on_one_tenant_are_different_keys() -> None:
    """A tenant may publish several sites; they must not share a cache slot."""
    one = WorkdayTenant("acme", "5", "A", "Acme")
    two = WorkdayTenant("acme", "5", "B", "Acme")

    assert one.key != two.key


@pytest.mark.parametrize("tenant", DEFAULT_TENANTS, ids=lambda t: t.key)
def test_every_default_tenant_builds_only_myworkdayjobs_urls(
    tenant: WorkdayTenant,
) -> None:
    for url in (tenant.host, tenant.site_url, tenant.jobs_url):
        assert url.startswith("https://")
        assert ".myworkdayjobs.com" in url
    # The one path every tenant's robots.txt closes.
    assert "/refreshFacet/" not in tenant.jobs_url


# --------------------------------------------------------------------------
# robots
# --------------------------------------------------------------------------


def test_a_real_tenant_robots_file_permits_the_cxs_path() -> None:
    portal, clients = build_portal(robots=ROBOTS["nvidia"])

    assert run(portal._robots_allows(TENANT)) is True


def test_robots_is_fetched_once_per_host_however_many_queries_run() -> None:
    portal, clients = build_portal(robots=ROBOTS["nvidia"])

    run(portal._robots_allows(TENANT))
    run(portal._robots_allows(TENANT))
    run(portal._robots_allows(TENANT))

    assert len(clients[TENANT.host].robots_calls) == 1


def test_a_tenant_that_disallows_its_site_is_declined() -> None:
    """mastercard closes /Public_Posting_Site/ while leaving the host open."""
    tenant = WorkdayTenant("mastercard", "1", "Public_Posting_Site", "Mastercard")
    portal, _ = build_portal([tenant], robots=ROBOTS["mastercard"])

    assert run(portal._robots_allows(tenant)) is False


def test_the_same_tenants_permitted_site_is_allowed() -> None:
    tenant = WorkdayTenant("mastercard", "1", "CorporateCareers", "Mastercard")
    portal, _ = build_portal([tenant], robots=ROBOTS["mastercard"])

    assert run(portal._robots_allows(tenant)) is True


def test_pfizer_internal_jobs_is_declined_but_its_careers_site_is_not() -> None:
    internal = WorkdayTenant("pfizer", "1", "InternalJobs", "Pfizer")
    external = WorkdayTenant("pfizer", "1", "PfizerCareers", "Pfizer")
    portal, _ = build_portal([internal, external], robots=ROBOTS["pfizer"])

    assert run(portal._robots_allows(internal)) is False
    assert run(portal._robots_allows(external)) is True


def test_a_missing_robots_file_means_no_restrictions_published() -> None:
    portal, _ = build_portal(robots=None)

    assert run(portal._robots_allows(TENANT)) is True


def test_an_unreadable_robots_file_declines_the_tenant() -> None:
    """The host has a policy and would not give it to us: decline."""
    portal, _ = build_portal(robots_error=PortalError("HTTP 500", portal="workday"))

    assert run(portal._robots_allows(TENANT)) is False


def test_a_blanket_disallow_declines_the_tenant() -> None:
    portal, _ = build_portal(robots="User-agent: *\nDisallow: /\n")

    assert run(portal._robots_allows(TENANT)) is False


def test_a_refusal_while_reading_robots_is_raised_not_swallowed() -> None:
    portal, _ = build_portal(
        robots_error=PortalBlockedError("refused", portal="workday")
    )

    with pytest.raises(PortalBlockedError):
        run(portal._robots_allows(TENANT))


def test_a_declined_tenant_is_skipped_without_any_further_request() -> None:
    portal, clients = build_portal(robots="User-agent: *\nDisallow: /\n")

    with pytest.raises(PortalBlockedError):
        run(portal.search(request_for("financial analyst")))

    client = clients[TENANT.host]
    assert client.list_calls == []
    assert client.detail_calls == []


def test_every_tenant_declining_is_reported_as_blocked() -> None:
    portal, _ = build_portal(robots="User-agent: *\nDisallow: /\n")

    with pytest.raises(PortalBlockedError):
        run(portal.search(request_for("financial analyst")))


# --------------------------------------------------------------------------
# Title matching
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# Relative labels and dates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Posted Today", False),
        ("Posted Yesterday", False),
        ("Posted 5 Days Ago", False),
        ("Posted 28 Days Ago", False),
        ("Posted 29 Days Ago", False),
        ("Posted 30+ Days Ago", True),
        (None, False),
        ("", False),
    ],
)
def test_only_the_explicit_thirty_plus_bucket_counts_as_stale(
    label: str | None, expected: bool
) -> None:
    assert is_stale_label(label) is expected


def test_the_stale_marker_is_the_one_workday_actually_publishes() -> None:
    assert STALE_POSTED_MARKER in (row("stale_row")["postedOn"])


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Posted Today", 0),
        ("Posted Yesterday", 1),
        ("Posted 6 Days Ago", 6),
        ("Posted 30+ Days Ago", 30),
        ("Posted recently", None),
        (None, None),
    ],
)
def test_relative_labels_are_read_but_never_turned_into_dates(
    label: str | None, expected: int | None
) -> None:
    assert relative_label_days(label) == expected


def test_start_date_is_read_as_an_aware_utc_timestamp() -> None:
    parsed = parse_start_date("2026-08-07")

    assert parsed == datetime(2026, 8, 7, tzinfo=UTC)
    assert parsed.tzinfo is not None


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "not-a-date", "2026-13-01", "07/08/2026", 20260807, [], {}, True],
)
def test_a_missing_or_malformed_start_date_yields_none(value: Any) -> None:
    assert parse_start_date(value) is None


def test_the_live_payload_start_date_matches_its_own_relative_label() -> None:
    """The proof the investigation rested on, pinned against a real payload."""
    record = DETAILS["us_single"]
    posted_at = parse_start_date(record["detail"]["jobPostingInfo"]["startDate"])
    stated = relative_label_days(record["row"]["postedOn"])

    assert posted_at is not None
    assert stated is not None
    captured = datetime.now(UTC)
    age_days = (captured - posted_at).days
    # The capture and this run are days apart, so the label can only have got
    # older. It must never be younger than the date claims.
    assert age_days >= stated


def test_a_listing_without_a_date_is_dropped_rather_than_dated() -> None:
    portal, _ = build_portal()
    payload = json.loads(json.dumps(detail("us_single")))
    payload["jobPostingInfo"].pop("startDate")

    assert portal._to_raw_job(payload, candidate=_candidate()) is None


def test_posted_at_feeds_the_real_freshness_gate() -> None:
    now = datetime(2026, 9, 15, tzinfo=UTC)

    assert is_eligible_age(parse_start_date("2026-09-10"), now) is True
    # Exactly thirty days old is inside the window; thirty-one is not.
    assert is_eligible_age(parse_start_date("2026-08-16"), now) is True
    assert is_eligible_age(parse_start_date("2026-08-15"), now) is False
    assert is_eligible_age(None, now) is False


def test_nothing_substitutes_the_current_time_for_a_publication_date() -> None:
    portal, _ = build_portal()
    payload = json.loads(json.dumps(detail("us_single")))
    payload["jobPostingInfo"]["startDate"] = "garbage"

    assert portal._to_raw_job(payload, candidate=_candidate()) is None


# --------------------------------------------------------------------------
# Field mapping
# --------------------------------------------------------------------------


def _candidate(name: str = "us_single", tenant: WorkdayTenant = TENANT):
    from app.portals.adapters.workday import _Candidate

    record = DETAILS[name]["row"]
    return _Candidate(
        tenant=tenant,
        title=record["title"],
        external_path=record["externalPath"],
        posted_on=record.get("postedOn"),
        locations_text=record.get("locationsText"),
    )


def test_a_live_payload_maps_onto_the_raw_job_contract() -> None:
    portal, _ = build_portal()

    job = portal._to_raw_job(detail("us_single"), candidate=_candidate())

    assert isinstance(job, RawJob)
    assert job.source == "workday"
    assert job.title == posting("us_single")["title"]
    assert job.external_id == posting("us_single")["jobReqId"]
    assert job.source_url == posting("us_single")["externalUrl"]
    assert job.application_url == posting("us_single")["externalUrl"].rstrip("/") + "/apply"
    assert job.posted_at == parse_start_date(posting("us_single")["startDate"])
    assert job.description and len(job.description) > 500
    assert job.employment_type == "Full-time"


def test_the_company_is_the_configured_display_name() -> None:
    tenant = WorkdayTenant(TENANT.tenant, TENANT.wd, TENANT.site, "U.S. Bank")
    portal, _ = build_portal([tenant])

    job = portal._to_raw_job(detail("us_single"), candidate=_candidate(tenant=tenant))

    assert job is not None
    assert job.company == "U.S. Bank"


def test_the_workday_legal_entity_is_never_the_company() -> None:
    """The defect this portal is configured around, asserted rather than trusted."""
    entity = (detail("us_single").get("hiringOrganization") or {}).get("name")
    tenant = WorkdayTenant(TENANT.tenant, TENANT.wd, TENANT.site, "U.S. Bank")
    portal, _ = build_portal([tenant])

    job = portal._to_raw_job(detail("us_single"), candidate=_candidate(tenant=tenant))

    assert job is not None
    assert job.company == "U.S. Bank"
    if entity:
        assert job.company != entity
    # Preserved for debugging, and only there.
    assert job.raw_payload["hiringOrganizationName"] == entity


def test_an_empty_legal_entity_cannot_blank_the_employer() -> None:
    """capitalone publishes an empty organisation name."""
    payload = json.loads(json.dumps(detail("us_single")))
    payload["hiringOrganization"] = {"name": ""}
    tenant = WorkdayTenant("capitalone", "12", "Capital_One", "Capital One")
    portal, _ = build_portal([tenant])

    job = portal._to_raw_job(payload, candidate=_candidate(tenant=tenant))

    assert job is not None
    assert job.company == "Capital One"


def test_a_missing_hiring_organization_block_is_harmless() -> None:
    payload = json.loads(json.dumps(detail("us_single")))
    payload.pop("hiringOrganization", None)
    portal, _ = build_portal()

    job = portal._to_raw_job(payload, candidate=_candidate())

    assert job is not None
    assert job.company == TENANT.display_name


def test_the_requisition_id_is_the_external_identity() -> None:
    portal, _ = build_portal()

    job = portal._to_raw_job(detail("us_single"), candidate=_candidate())

    assert job is not None
    assert job.external_id
    assert job.external_id == posting("us_single")["jobReqId"]


def test_a_missing_requisition_id_leaves_the_identity_unset() -> None:
    payload = json.loads(json.dumps(detail("us_single")))
    payload["jobPostingInfo"].pop("jobReqId")
    portal, _ = build_portal()

    job = portal._to_raw_job(payload, candidate=_candidate())

    assert job is not None
    assert job.external_id is None
    # The URL still identifies it, which is what collection falls back to.
    assert job.source_url


def test_a_missing_external_url_is_rebuilt_from_the_site_and_path() -> None:
    payload = json.loads(json.dumps(detail("us_single")))
    payload["jobPostingInfo"].pop("externalUrl")
    portal, _ = build_portal()

    job = portal._to_raw_job(payload, candidate=_candidate())

    assert job is not None
    assert job.source_url.startswith(f"{TENANT.host}/{TENANT.site}/job/")


def test_the_application_url_is_the_posting_plus_apply() -> None:
    portal, _ = build_portal()

    job = portal._to_raw_job(detail("us_single"), candidate=_candidate())

    assert job is not None
    assert job.application_url == f"{job.source_url}/apply"


def test_the_application_type_is_declared() -> None:
    portal, _ = build_portal()

    job = portal._to_raw_job(detail("us_single"), candidate=_candidate())

    assert job is not None
    assert job.raw_payload["application_type"] == ApplicationType.LONG_APPLICATION.value


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("Full time", "Full-time"),
        ("Part time", "Part-time"),
        ("Intern", "Intern"),
        ("Something Else", "Something Else"),
        (None, None),
        ("", None),
        (42, None),
    ],
)
def test_time_type_maps_to_employment_type(published: Any, expected: Any) -> None:
    assert employment_type({"timeType": published}) == expected


def test_a_payload_with_no_posting_info_is_dropped() -> None:
    portal, _ = build_portal()

    assert portal._to_raw_job({"hiringOrganization": {}}, candidate=_candidate()) is None
    assert portal._to_raw_job({"jobPostingInfo": []}, candidate=_candidate()) is None


def test_a_payload_with_no_title_anywhere_is_dropped() -> None:
    portal, _ = build_portal()
    payload = json.loads(json.dumps(detail("us_single")))
    payload["jobPostingInfo"]["title"] = ""
    from app.portals.adapters.workday import _Candidate

    blank = _Candidate(
        tenant=TENANT, title="", external_path="/job/x", posted_on=None, locations_text=None
    )

    assert portal._to_raw_job(payload, candidate=blank) is None


# --------------------------------------------------------------------------
# Description
# --------------------------------------------------------------------------


def test_the_description_is_the_full_posting_text() -> None:
    text = build_description(posting("us_single"))

    assert text is not None
    assert len(text) > 1000


def test_the_description_preserves_structure_as_lines() -> None:
    text = html_to_text(
        "<p>About</p><h2>Responsibilities</h2><ul><li>One</li><li>Two</li></ul>"
        "<p>Qualifications</p>"
    )

    assert text is not None
    assert "Responsibilities" in text
    assert "- One" in text
    assert "- Two" in text
    assert "Qualifications" in text


def test_the_description_decodes_entities_and_drops_markup() -> None:
    text = html_to_text("<p>Salary &amp; benefits &lt;competitive&gt;</p>")

    assert text == "Salary & benefits <competitive>"


@pytest.mark.parametrize("value", [None, "", "   ", "<p></p>", 42, [], {}])
def test_an_empty_description_is_none_not_an_empty_string(value: Any) -> None:
    assert html_to_text(value) is None


def test_a_posting_with_no_description_is_dropped() -> None:
    portal, _ = build_portal()
    payload = json.loads(json.dumps(detail("us_single")))
    payload["jobPostingInfo"]["jobDescription"] = ""

    assert portal._to_raw_job(payload, candidate=_candidate()) is None


def test_the_description_is_not_truncated() -> None:
    published = posting("us_single")["jobDescription"]
    text = build_description(posting("us_single"))

    assert text is not None
    # Every non-markup word of the source survives.
    words = [w for w in html_to_text(published).split() if len(w) > 6]
    assert all(word in text for word in words[:40])


def test_the_legal_entity_is_never_injected_into_the_description() -> None:
    entity = (detail("us_single").get("hiringOrganization") or {}).get("name")
    text = build_description(posting("us_single"))

    assert text is not None
    if entity and entity not in html_to_text(posting("us_single")["jobDescription"]):
        assert entity not in text


# --------------------------------------------------------------------------
# Work mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ("Remote", "Remote"),
        ("remote", "Remote"),
        ("Hybrid", "Hybrid"),
        ("Office", "Onsite"),
        ("Onsite", "Onsite"),
        (None, None),
        ("", None),
    ],
)
def test_remote_type_is_canonicalised_where_unambiguous(
    published: Any, expected: Any
) -> None:
    assert workplace_label({"remoteType": published}) == expected


def test_an_unrecognised_arrangement_passes_through_as_published() -> None:
    """Salesforce's "Office - Flexible" is not forced into a guess."""
    assert workplace_label({"remoteType": "Office - Flexible"}) == "Office - Flexible"


def test_an_unrecognised_arrangement_yields_no_work_mode_claim() -> None:
    text = build_description({"jobDescription": "<p>Body</p>", "remoteType": "Office - Flexible"})

    assert text is not None
    assert detect_work_mode(text[:2000]) is None


def test_a_remote_posting_reaches_the_shared_work_mode_detector() -> None:
    from app.core.constants import WorkMode

    text = build_description(posting("remote_mode"))

    assert text is not None
    assert text.startswith("Workplace type: Remote")
    assert detect_work_mode(text[:2000]) is WorkMode.REMOTE


def test_a_hybrid_posting_reaches_the_shared_work_mode_detector() -> None:
    from app.core.constants import WorkMode

    text = build_description(posting("hybrid_mode"))

    assert text is not None
    assert text.startswith("Workplace type: Hybrid")
    assert detect_work_mode(text[:2000]) is WorkMode.HYBRID


def test_a_posting_with_no_remote_type_makes_no_work_mode_claim() -> None:
    text = build_description({"jobDescription": "<p>Body</p>"})

    assert text == "Body"


# --------------------------------------------------------------------------
# Location and the United States decision
# --------------------------------------------------------------------------


def test_the_structured_country_is_read_from_the_requisition_location() -> None:
    assert country_code(posting("us_single")) == "US"
    assert country_code(posting("non_us")) == "CA"


def test_a_posting_with_no_country_anywhere_reads_as_unknown() -> None:
    assert country_code({}) is None
    assert is_us_posting({}) is False


@pytest.mark.parametrize(
    "code", ["US", "us", "USA", "United States", "United States of America"]
)
def test_every_spelling_of_the_united_states_is_accepted(code: str) -> None:
    posting_ = {"jobRequisitionLocation": {"country": {"alpha2Code": code}}}

    assert is_us_posting(posting_) is True


@pytest.mark.parametrize("code", ["CA", "GB", "IN", "PL", "Canada", "India"])
def test_other_countries_are_not_the_united_states(code: str) -> None:
    posting_ = {"jobRequisitionLocation": {"country": {"alpha2Code": code}}}

    assert is_us_posting(posting_) is False


def test_the_location_string_carries_the_published_country() -> None:
    formatted = format_location(posting("us_single"))

    assert formatted is not None
    assert formatted.endswith("United States of America")


@pytest.mark.parametrize(
    "descriptor",
    [
        "VA-Chesapeake",
        "7000 Target Pkwy N,NCD-0000",
        "US, CA, Santa Clara",
        "Work At Home-Connecticut",
        "Sysco Minnesota",
        "MS - Kosciusko",
    ],
)
def test_real_tenant_descriptors_classify_as_united_states_once_formatted(
    descriptor: str,
) -> None:
    """Two of these defeat the shared classifier on their own.

    ``VA-Chesapeake`` and the bare street address both come back unclassified
    raw, which would throw away real United States jobs. Appending the
    published country - a structured field, not a guess - is what the adapter
    does instead of touching ``us_locations.py``.
    """
    posting_ = {
        "jobRequisitionLocation": {
            "descriptor": descriptor,
            "country": {
                "alpha2Code": "US",
                "descriptor": "United States of America",
            },
        }
    }

    formatted = format_location(posting_)

    assert formatted is not None
    assert classify_us_location(formatted).is_us is True


@pytest.mark.parametrize(
    ("descriptor", "country"),
    [("Canada - Toronto", "Canada"), ("India - Hyderabad", "India"), ("Poland - Warsaw", "Poland")],
)
def test_formatting_never_turns_a_foreign_posting_into_a_united_states_one(
    descriptor: str, country: str
) -> None:
    posting_ = {
        "jobRequisitionLocation": {
            "descriptor": descriptor,
            "country": {"alpha2Code": country[:2].upper(), "descriptor": country},
        }
    }

    formatted = format_location(posting_)

    assert formatted is not None
    assert classify_us_location(formatted).is_us is False


def test_a_country_already_stated_is_not_repeated() -> None:
    posting_ = {
        "jobRequisitionLocation": {
            "descriptor": "Chicago, United States of America",
            "country": {"descriptor": "United States of America"},
        }
    }

    assert format_location(posting_) == "Chicago, United States of America"


def test_the_location_falls_back_to_the_flat_field() -> None:
    assert format_location({"location": "Boston"}) == "Boston"


def test_a_posting_with_no_location_at_all_is_none() -> None:
    assert format_location({}) is None


def test_additional_locations_are_read_as_published() -> None:
    others = additional_locations(posting("multi_us"))

    assert others
    assert all(isinstance(value, str) for value in others)


@pytest.mark.parametrize("value", [None, "Toronto", 42, {}, [1, 2], ["", "  "]])
def test_malformed_additional_locations_are_ignored(value: Any) -> None:
    assert additional_locations({"additionalLocations": value}) == ()


def test_a_multi_location_role_is_judged_on_its_structured_primary_only() -> None:
    """Conservative by choice: the extra locations carry no country.

    Reading a country out of "Canada - Montreal" would be inference, which
    this project does not do. A role Workday placed in Canada stays Canadian
    even when a secondary office reads American.
    """
    record = posting("multi_non_us")

    assert additional_locations(record)
    assert is_us_posting(record) is False
    formatted = format_location(record)
    assert formatted is not None
    assert classify_us_location(formatted).is_us is False


def test_a_multi_location_united_states_role_keeps_its_other_offices_visible() -> None:
    record = posting("multi_us")
    text = build_description(record)

    assert is_us_posting(record) is True
    assert text is not None
    assert "Additional locations:" in text


def test_the_adapter_does_not_filter_by_country_itself() -> None:
    """The pipeline decides; the adapter formats. A non-US job still maps."""
    portal, _ = build_portal()

    job = portal._to_raw_job(detail("non_us"), candidate=_candidate("non_us"))

    assert job is not None
    assert classify_us_location(job.location).is_us is False


# --------------------------------------------------------------------------
# Listing, pagination and caching
# --------------------------------------------------------------------------


def _list_page(rows: list[dict], total: int) -> dict[str, Any]:
    return {"total": total, "jobPostings": rows, "userAuthenticated": False}


def _make_row(index: int, *, title: str = "Financial Analyst", posted: str = "Posted Today") -> dict:
    return {
        "title": title,
        "externalPath": f"/job/Somewhere/{title.replace(' ', '-')}_{index}",
        "locationsText": "Somewhere",
        "postedOn": posted,
        "bulletFields": [f"R{index}"],
    }


def _detail_for(path: str, *, title: str = "Financial Analyst") -> dict[str, Any]:
    payload = json.loads(json.dumps(detail("us_single")))
    payload["jobPostingInfo"]["title"] = title
    payload["jobPostingInfo"]["jobReqId"] = path.rsplit("_", 1)[-1]
    payload["jobPostingInfo"]["externalUrl"] = f"{TENANT.host}/{TENANT.site}{path}"
    payload["jobPostingInfo"]["startDate"] = (
        datetime.now(UTC) - timedelta(days=2)
    ).date().isoformat()
    return payload


def test_the_request_body_is_the_documented_shape() -> None:
    portal, clients = build_portal(lists={("financial analyst", 0): _list_page([], 0)})

    run(portal.search(request_for("financial analyst")))

    assert clients[TENANT.host].list_calls == [
        f"LIST {TENANT.jobs_url} financial analyst@0"
    ]


def test_the_page_size_is_the_one_the_endpoint_actually_serves() -> None:
    """Larger values return zero rows and a null total - measured, not guessed."""
    assert PAGE_SIZE == 20


def test_pagination_walks_until_the_reported_total_is_reached() -> None:
    rows_a = [_make_row(i) for i in range(PAGE_SIZE)]
    rows_b = [_make_row(i + PAGE_SIZE) for i in range(5)]
    lists = {
        ("financial analyst", 0): _list_page(rows_a, 25),
        ("financial analyst", PAGE_SIZE): _list_page(rows_b, 25),
    }
    details = {
        TENANT.detail_url(r["externalPath"]): _detail_for(r["externalPath"])
        for r in rows_a + rows_b
    }
    portal, clients = build_portal(lists=lists, details=details)

    result = run(portal.search(request_for("financial analyst")))

    assert len(clients[TENANT.host].list_calls) == 2
    assert len(result.jobs) == 25


def test_pagination_stops_on_the_total_not_on_an_empty_page() -> None:
    """An out-of-range offset returns rows again, so emptiness never arrives."""
    rows = [_make_row(i) for i in range(PAGE_SIZE)]
    lists = {("financial analyst", offset): _list_page(rows, 20) for offset in (0, 20, 40)}
    details = {
        TENANT.detail_url(r["externalPath"]): _detail_for(r["externalPath"]) for r in rows
    }
    portal, clients = build_portal(lists=lists, details=details)

    run(portal.search(request_for("financial analyst")))

    # total is 20 and the first page held 20, so there is no second page.
    assert len(clients[TENANT.host].list_calls) == 1


def test_pagination_is_bounded_however_large_the_board() -> None:
    rows = [_make_row(i) for i in range(PAGE_SIZE)]
    lists = {
        ("financial analyst", offset * PAGE_SIZE): _list_page(rows, 100_000)
        for offset in range(10)
    }
    details = {
        TENANT.detail_url(r["externalPath"]): _detail_for(r["externalPath"]) for r in rows
    }
    portal, clients = build_portal(lists=lists, details=details)

    run(portal.search(request_for("financial analyst")))

    assert len(clients[TENANT.host].list_calls) == MAX_PAGES_PER_QUERY


def test_duplicate_rows_within_one_page_are_collapsed() -> None:
    duplicate = _make_row(1)
    lists = {("financial analyst", 0): _list_page([duplicate, duplicate, duplicate], 3)}
    details = {
        TENANT.detail_url(duplicate["externalPath"]): _detail_for(
            duplicate["externalPath"]
        )
    }
    portal, clients = build_portal(lists=lists, details=details)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1
    assert len(clients[TENANT.host].detail_calls) == 1


def test_a_list_page_is_fetched_once_per_query_and_offset() -> None:
    lists = {("financial analyst", 0): _list_page([], 0)}
    portal, clients = build_portal(lists=lists)

    run(portal.search(request_for("financial analyst")))
    run(portal.search(request_for("financial analyst")))

    assert len(clients[TENANT.host].list_calls) == 1


# --------------------------------------------------------------------------
# The request-count regression: overlapping variations
# --------------------------------------------------------------------------


def test_one_posting_answering_several_variations_costs_one_detail_fetch() -> None:
    """The regression this portal most needs guarding.

    The collector calls ``search`` once per same-role variation, and Workday's
    fuzzy ``searchText`` returns overlapping rows for synonymous queries. A
    posting that answers three of them must still be fetched once.
    """
    shared = _make_row(1, title="Senior Financial Analyst")
    variations = ["financial analyst", "senior financial analyst", "finance analyst"]
    lists = {(query, 0): _list_page([shared], 1) for query in variations}
    details = {
        TENANT.detail_url(shared["externalPath"]): _detail_for(
            shared["externalPath"], title="Senior Financial Analyst"
        )
    }
    portal, clients = build_portal(lists=lists, details=details)

    for query in variations:
        run(portal.search(request_for(query)))

    client = clients[TENANT.host]
    # One list call per distinct query, because the searches differ.
    assert len(client.list_calls) == len(variations)
    # But exactly one detail call, because it is the same posting.
    assert len(client.detail_calls) == 1


def test_overlapping_variations_within_one_call_also_fetch_once() -> None:
    shared = _make_row(1, title="Senior Financial Analyst")
    lists = {
        ("financial analyst", 0): _list_page([shared], 1),
        ("senior financial analyst", 0): _list_page([shared], 1),
    }
    details = {
        TENANT.detail_url(shared["externalPath"]): _detail_for(
            shared["externalPath"], title="Senior Financial Analyst"
        )
    }
    portal, clients = build_portal(lists=lists, details=details)

    result = run(
        portal.search(request_for("financial analyst", "senior financial analyst"))
    )

    assert len(result.jobs) == 1
    assert len(clients[TENANT.host].detail_calls) == 1


def test_a_stale_row_never_costs_a_detail_request() -> None:
    fresh = _make_row(1)
    stale = _make_row(2, posted="Posted 30+ Days Ago")
    lists = {("financial analyst", 0): _list_page([fresh, stale], 2)}
    details = {
        TENANT.detail_url(fresh["externalPath"]): _detail_for(fresh["externalPath"]),
        TENANT.detail_url(stale["externalPath"]): _detail_for(stale["externalPath"]),
    }
    portal, clients = build_portal(lists=lists, details=details)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1
    assert clients[TENANT.host].detail_calls == [
        f"DETAIL {TENANT.detail_url(fresh['externalPath'])}"
    ]


def test_a_title_that_does_not_match_never_costs_a_detail_request() -> None:
    wanted = _make_row(1, title="Financial Analyst")
    other = _make_row(2, title="Warehouse Associate")
    lists = {("financial analyst", 0): _list_page([wanted, other], 2)}
    details = {
        TENANT.detail_url(wanted["externalPath"]): _detail_for(wanted["externalPath"])
    }
    portal, clients = build_portal(lists=lists, details=details)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1
    assert len(clients[TENANT.host].detail_calls) == 1


def test_the_result_limit_bounds_how_many_details_are_opened() -> None:
    rows = [_make_row(i) for i in range(PAGE_SIZE)]
    lists = {("financial analyst", 0): _list_page(rows, PAGE_SIZE)}
    details = {
        TENANT.detail_url(r["externalPath"]): _detail_for(r["externalPath"]) for r in rows
    }
    portal, clients = build_portal(lists=lists, details=details)

    result = run(portal.search(request_for("financial analyst", limit=5)))

    assert len(result.jobs) == 5
    assert len(clients[TENANT.host].detail_calls) == 5


# --------------------------------------------------------------------------
# Multiple tenants, isolation and failure
# --------------------------------------------------------------------------

TENANT_A = WorkdayTenant("acme", "1", "Careers", "Acme Corp")
TENANT_B = WorkdayTenant("brandco", "5", "Jobs", "Brand Co")


def _two_tenant_portal(**per_host: FakeClient) -> tuple[WorkdayPortal, dict[str, FakeClient]]:
    clients = {TENANT_A.host: per_host["a"], TENANT_B.host: per_host["b"]}
    portal, made = build_portal([TENANT_A, TENANT_B], clients=clients)
    return portal, made


def test_each_tenant_gets_its_own_client_so_pacing_is_per_host() -> None:
    portal = WorkdayPortal(
        {"tenants": [TENANT_A.as_dict(), TENANT_B.as_dict()]}
    )

    first = portal._client_for(TENANT_A)
    second = portal._client_for(TENANT_B)
    again = portal._client_for(TENANT_A)

    assert first is not second
    assert first is again


def test_two_tenants_both_contribute() -> None:
    row_a = _make_row(1)
    row_b = _make_row(2)
    client_a = FakeClient(
        robots=ALLOW_ALL,
        lists={("financial analyst", 0): _list_page([row_a], 1)},
        details={TENANT_A.detail_url(row_a["externalPath"]): _detail_for(row_a["externalPath"])},
    )
    client_b = FakeClient(
        robots=ALLOW_ALL,
        lists={("financial analyst", 0): _list_page([row_b], 1)},
        details={TENANT_B.detail_url(row_b["externalPath"]): _detail_for(row_b["externalPath"])},
    )
    portal, _ = _two_tenant_portal(a=client_a, b=client_b)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 2
    assert {job.company for job in result.jobs} == {"Acme Corp", "Brand Co"}


def test_one_tenant_failing_does_not_cost_the_others() -> None:
    row_b = _make_row(2)
    client_a = FakeClient(robots=ALLOW_ALL, list_error=PortalError("down", portal="workday"))
    client_b = FakeClient(
        robots=ALLOW_ALL,
        lists={("financial analyst", 0): _list_page([row_b], 1)},
        details={TENANT_B.detail_url(row_b["externalPath"]): _detail_for(row_b["externalPath"])},
    )
    portal, _ = _two_tenant_portal(a=client_a, b=client_b)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1
    assert result.jobs[0].company == "Brand Co"


def test_one_tenant_declined_by_robots_does_not_cost_the_others() -> None:
    row_b = _make_row(2)
    client_a = FakeClient(robots="User-agent: *\nDisallow: /\n")
    client_b = FakeClient(
        robots=ALLOW_ALL,
        lists={("financial analyst", 0): _list_page([row_b], 1)},
        details={TENANT_B.detail_url(row_b["externalPath"]): _detail_for(row_b["externalPath"])},
    )
    portal, _ = _two_tenant_portal(a=client_a, b=client_b)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1
    assert client_a.list_calls == []


def test_every_tenant_failing_is_a_portal_error() -> None:
    client_a = FakeClient(robots=ALLOW_ALL, list_error=PortalError("down", portal="workday"))
    client_b = FakeClient(robots=ALLOW_ALL, list_error=PortalError("down", portal="workday"))
    portal, _ = _two_tenant_portal(a=client_a, b=client_b)

    with pytest.raises(PortalError, match="No Workday tenant could be read"):
        run(portal.search(request_for("financial analyst")))


def test_a_mix_of_failure_and_decline_with_nothing_left_is_an_error() -> None:
    client_a = FakeClient(robots="User-agent: *\nDisallow: /\n")
    client_b = FakeClient(robots=ALLOW_ALL, list_error=PortalError("down", portal="workday"))
    portal, _ = _two_tenant_portal(a=client_a, b=client_b)

    with pytest.raises(PortalError):
        run(portal.search(request_for("financial analyst")))


def test_a_tenant_returning_nothing_is_an_empty_success_not_a_failure() -> None:
    portal, _ = build_portal(lists={("financial analyst", 0): _list_page([], 0)})

    result = run(portal.search(request_for("financial analyst")))

    assert result.jobs == []
    assert result.window is FreshnessWindow.LAST_30D


def test_a_rate_limit_from_one_tenant_stops_the_whole_portal() -> None:
    """A refusal is respected rather than routed around."""
    client_a = FakeClient(
        robots=ALLOW_ALL, list_error=PortalRateLimitedError("slow down", portal="workday")
    )
    client_b = FakeClient(robots=ALLOW_ALL, lists={("financial analyst", 0): _list_page([], 0)})
    portal, _ = _two_tenant_portal(a=client_a, b=client_b)

    with pytest.raises(PortalRateLimitedError):
        run(portal.search(request_for("financial analyst")))


def test_a_block_from_one_tenant_stops_the_whole_portal() -> None:
    client_a = FakeClient(
        robots=ALLOW_ALL, list_error=PortalBlockedError("refused", portal="workday")
    )
    client_b = FakeClient(robots=ALLOW_ALL, lists={("financial analyst", 0): _list_page([], 0)})
    portal, _ = _two_tenant_portal(a=client_a, b=client_b)

    with pytest.raises(PortalBlockedError):
        run(portal.search(request_for("financial analyst")))


def test_a_timeout_is_reported_as_a_portal_failure() -> None:
    portal, _ = build_portal(list_error=PortalTimeoutError("too slow", portal="workday"))

    with pytest.raises(PortalError):
        run(portal.search(request_for("financial analyst")))


def test_a_dns_failure_is_reported_as_a_portal_failure() -> None:
    portal, _ = build_portal(
        robots_error=PortalError("Network error: ConnectError", portal="workday"),
        list_error=PortalError("Network error: ConnectError", portal="workday"),
    )

    # robots was unreadable, so the tenant declined; every tenant declined.
    with pytest.raises(PortalError):
        run(portal.search(request_for("financial analyst")))


def test_a_list_payload_without_a_postings_list_is_a_parse_error() -> None:
    portal, _ = build_portal(lists={("financial analyst", 0): {"total": 3}})

    with pytest.raises(PortalError):
        run(portal.search(request_for("financial analyst")))


def test_a_malformed_list_row_is_skipped_not_fatal() -> None:
    good = _make_row(1)
    lists = {
        ("financial analyst", 0): _list_page(
            ["not a dict", {}, {"title": "Financial Analyst"}, good], 4
        )
    }
    details = {TENANT.detail_url(good["externalPath"]): _detail_for(good["externalPath"])}
    portal, _ = build_portal(lists=lists, details=details)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1


def test_a_malformed_detail_payload_drops_that_job_only() -> None:
    good = _make_row(1)
    bad = _make_row(2)
    lists = {("financial analyst", 0): _list_page([good, bad], 2)}
    details = {
        TENANT.detail_url(good["externalPath"]): _detail_for(good["externalPath"]),
        TENANT.detail_url(bad["externalPath"]): {"nonsense": True},
    }
    portal, _ = build_portal(lists=lists, details=details)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1


def test_a_detail_request_failing_drops_that_job_only() -> None:
    good = _make_row(1)
    missing = _make_row(2)
    lists = {("financial analyst", 0): _list_page([good, missing], 2)}
    details = {TENANT.detail_url(good["externalPath"]): _detail_for(good["externalPath"])}
    portal, _ = build_portal(lists=lists, details=details)

    result = run(portal.search(request_for("financial analyst")))

    assert len(result.jobs) == 1


def test_a_portal_with_no_tenants_configured_raises_rather_than_returning_empty() -> None:
    portal = WorkdayPortal()
    portal._tenants = ()

    with pytest.raises(PortalError, match="No Workday tenants are configured"):
        run(portal.search(request_for("financial analyst")))


# --------------------------------------------------------------------------
# Result shape and the contract
# --------------------------------------------------------------------------


def test_the_window_is_never_claimed_to_be_applied_by_the_portal() -> None:
    portal, _ = build_portal(lists={("financial analyst", 0): _list_page([], 0)})

    result = run(portal.search(request_for("financial analyst")))

    assert result.window_applied_by_portal is False
    assert result.has_more is False
    assert result.next_cursor is None


def test_fetch_description_returns_what_search_already_collected() -> None:
    portal, _ = build_portal()
    job = portal._to_raw_job(detail("us_single"), candidate=_candidate())

    assert job is not None
    assert run(portal.fetch_description(job)) == job.description


def test_validate_url_probes_the_owning_tenants_client() -> None:
    portal, clients = build_portal()
    url = f"{TENANT.host}/{TENANT.site}/job/x"

    assert run(portal.validate_url(url)) is True
    assert clients[TENANT.host].requests == [f"HEAD {url}"]


def test_health_check_reports_rather_than_raising() -> None:
    portal, _ = build_portal(list_error=PortalError("down", portal="workday"))

    health = run(portal.health_check())

    assert health.is_available is False
    assert health.detail


def test_health_check_passes_when_a_tenant_answers() -> None:
    portal, _ = build_portal(lists={("", 0): _list_page([], 0)})

    assert run(portal.health_check()).is_available is True


def test_health_check_reports_a_robots_decline_honestly() -> None:
    portal, _ = build_portal(robots="User-agent: *\nDisallow: /\n")

    health = run(portal.health_check())

    assert health.is_available is False
    assert "robots" in (health.detail or "")


# --------------------------------------------------------------------------
# Dedup and URL classification
# --------------------------------------------------------------------------


def test_workday_urls_already_classify_as_ats() -> None:
    """No change to url classification was needed; this pins that."""
    for url in (
        f"{TENANT.host}/{TENANT.site}/job/Chicago/Financial-Analyst_R1",
        "https://acme.wd3.myworkdaysite.com/Careers/job/X/Analyst_R2",
    ):
        assert classify_url(url) is JobUrlKind.ATS


def test_two_postings_from_one_employer_share_a_canonical_company() -> None:
    """The fragmentation the configured display_name exists to prevent.

    Workday returns different legal entities for the same employer - U.S. Bank
    publishes both "300 U.S. Bank National Association" and "417 U.S. Bank
    Trust". Grouping is on the company, so two entities would be two employers.
    """
    from app.processing.normalize import normalize_raw_job

    tenant = WorkdayTenant("usbank", "1", "US_Bank_Careers", "U.S. Bank")
    portal, _ = build_portal([tenant])

    first = json.loads(json.dumps(detail("us_single")))
    first["hiringOrganization"] = {"name": "300 U.S. Bank National Association"}
    first["jobPostingInfo"]["title"] = "Financial Analyst"
    second = json.loads(json.dumps(detail("us_single")))
    second["hiringOrganization"] = {"name": "417 U.S. Bank Trust"}
    second["jobPostingInfo"]["title"] = "Financial Analyst"

    job_one = portal._to_raw_job(first, candidate=_candidate(tenant=tenant))
    job_two = portal._to_raw_job(second, candidate=_candidate(tenant=tenant))

    assert job_one is not None and job_two is not None
    assert job_one.company == job_two.company == "U.S. Bank"

    first = normalize_raw_job(job_one)
    second = normalize_raw_job(job_two)

    assert first.canonical_key == second.canonical_key
