"""Scale.jobs adapter: parsing, URL generation, pagination, failures.

Every response here comes from the captured fixture or a stub. Nothing in this
file makes a network request, and the constraints this project applies at the
request itself - no LinkedIn, no Easy Apply, no administrative endpoint - are
asserted against the parameters actually sent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import FRESHNESS_ESCALATION, FreshnessWindow
from app.portals.adapters.scalejobs import (
    MAX_PAGES,
    PAGE_SIZE,
    PUBLIC_USER_ID,
    SEARCH_URL,
    ScaleJobsPortal,
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
    Path(__file__).parent / "fixtures" / "portals" / "scalejobs_financial_analyst.json"
)

QUERY = "Financial Analyst"


@pytest.fixture(scope="module")
def captured() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def records(captured: dict[str, Any]) -> list[dict[str, Any]]:
    """The first captured page of listings."""
    return captured[QUERY][0]["jobs"]


@pytest.fixture
def stub_search(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]):
    """Serve search pages from the fixture, recording every request."""

    def _apply(payload_for=None, pages=captured):
        calls: list[dict[str, Any]] = []

        async def _get_json(self, url, *, params=None):  # noqa: ANN001
            params = dict(params or {})
            calls.append({"url": url, "params": params})
            if payload_for is not None:
                return payload_for(url, params)

            captured_pages = pages.get(params.get("jobTitles"))
            if captured_pages is None:
                return {"jobs": [], "hasMore": False}
            index = int(params.get("skip", 0)) // PAGE_SIZE
            if index >= len(captured_pages):
                return {"jobs": [], "hasMore": False}
            return captured_pages[index]

        monkeypatch.setattr("app.portals.http.PortalHttpClient.get_json", _get_json)
        return calls

    return _apply


def _portal(**options) -> ScaleJobsPortal:
    return ScaleJobsPortal(dict(options))


async def _search(
    adapter: ScaleJobsPortal,
    *,
    queries: tuple[str, ...] = (QUERY,),
    window: FreshnessWindow = FreshnessWindow.OLDER_ACTIVE,
    limit: int = 100,
    **kwargs,
):
    return await adapter.search(
        PortalSearchRequest(queries=queries, window=window, limit=limit, **kwargs)
    )


# --- Capabilities -----------------------------------------------------------


def test_the_adapter_declares_what_it_can_actually_do() -> None:
    capabilities = _portal().capabilities

    # `dateFrom` genuinely filters, so windows are requested, not filtered
    # locally. There is no per-job endpoint, so nothing is left to fetch.
    assert capabilities.supports_date_filter is True
    assert capabilities.supports_description_fetch is False
    assert capabilities.supports_pagination is True
    assert capabilities.requires_auth is False
    assert capabilities.provides_application_url is True


def test_the_slug_matches_the_seeded_portal_row() -> None:
    from app.services.portal_seed import PORTAL_SEEDS

    assert ScaleJobsPortal.slug == "scalejobs"
    assert ScaleJobsPortal.slug in {seed.slug for seed in PORTAL_SEEDS}


# --- Access constraints, asserted at the request ----------------------------


async def test_every_request_excludes_linkedin(stub_search) -> None:
    """This project does not collect LinkedIn, so the flag is unconditional.

    The publisher's own docs say the inventory carries LinkedIn-sourced jobs.
    Omitting this parameter would collect them.
    """
    calls = stub_search()

    await _search(_portal())

    assert calls
    for call in calls:
        assert call["params"]["excludeLinkedIn"] == "true"


async def test_no_request_re_enables_easy_apply(stub_search) -> None:
    """Easy Apply listings are excluded by the endpoint's default.

    ``excludeEasyApply=false`` would opt back in; it must never be sent.
    """
    calls = stub_search()

    await _search(_portal())

    for call in calls:
        assert "excludeEasyApply" not in call["params"]


async def test_only_the_read_only_search_endpoint_is_requested(stub_search) -> None:
    """The same host exposes sync, cron and expiry endpoints that mutate data.

    None of them may ever be called.
    """
    calls = stub_search()

    await _search(_portal())

    assert calls
    for call in calls:
        assert call["url"] == SEARCH_URL
        assert "/sync/" not in call["url"]
        assert "/cron/" not in call["url"]
        assert "/expired" not in call["url"]
        assert "/embeddings/" not in call["url"]


async def test_nothing_is_requested_from_the_scale_jobs_site_host(stub_search) -> None:
    """scale.jobs' own robots.txt disallows /api/; that host is never touched."""
    calls = stub_search()

    await _search(_portal())

    for call in calls:
        assert "//scale.jobs" not in call["url"]
        assert call["url"].startswith("https://jobhose-prod.scale.jobs/")


async def test_the_public_search_identity_is_sent(stub_search) -> None:
    """The endpoint is called exactly as the site's own client calls it."""
    calls = stub_search()

    await _search(_portal())

    for call in calls:
        assert call["params"]["userId"] == PUBLIC_USER_ID
        assert call["params"]["source"] == "live"


# --- URL generation ---------------------------------------------------------


async def test_the_query_is_sent_as_the_job_title(stub_search) -> None:
    calls = stub_search()

    await _search(_portal(), queries=("FP&A Analyst",))

    assert calls[0]["params"]["jobTitles"] == "FP&A Analyst"


async def test_a_comma_in_a_title_does_not_become_two_searches(stub_search) -> None:
    """``jobTitles`` is comma-separated, so a comma would split one title."""
    calls = stub_search()

    await _search(_portal(), queries=("Analyst, Financial Planning",))

    assert "," not in calls[0]["params"]["jobTitles"]
    assert calls[0]["params"]["jobTitles"] == "Analyst Financial Planning"


async def test_a_blank_query_issues_no_request(stub_search) -> None:
    calls = stub_search()

    result = await _search(_portal(), queries=("   ",))

    assert calls == []
    assert result.jobs == []


async def test_each_query_is_issued_independently(stub_search) -> None:
    calls = stub_search()

    await _search(_portal(), queries=(QUERY, "Senior Financial Analyst"))

    titles = {call["params"]["jobTitles"] for call in calls}
    assert titles == {QUERY, "Senior Financial Analyst"}


# --- Freshness windows ------------------------------------------------------


@pytest.mark.parametrize(
    ("window", "days"),
    [
        (FreshnessWindow.LAST_24H, 1),
        (FreshnessWindow.LAST_3D, 3),
        (FreshnessWindow.LAST_7D, 7),
        (FreshnessWindow.LAST_14D, 14),
        (FreshnessWindow.LAST_30D, 30),
    ],
)
async def test_a_dated_window_is_asked_for_by_date(
    stub_search, window: FreshnessWindow, days: int
) -> None:
    calls = stub_search()

    await _search(_portal(), window=window)

    sent = datetime.fromisoformat(calls[0]["params"]["dateFrom"]).date()
    expected = (datetime.now(UTC) - timedelta(days=days)).date()
    assert sent == expected


async def test_the_oldest_window_sends_no_date_filter(stub_search) -> None:
    """OLDER_ACTIVE has no lower bound, so it must not send one."""
    calls = stub_search()

    await _search(_portal(), window=FreshnessWindow.OLDER_ACTIVE)

    for call in calls:
        assert "dateFrom" not in call["params"]


async def test_the_result_reports_the_window_was_applied_by_the_portal(
    stub_search,
) -> None:
    stub_search()

    result = await _search(_portal(), window=FreshnessWindow.LAST_7D)

    assert result.window is FreshnessWindow.LAST_7D
    assert result.window_applied_by_portal is True


@pytest.mark.parametrize("window", FRESHNESS_ESCALATION)
async def test_every_window_is_requestable(stub_search, window) -> None:
    stub_search()

    result = await _search(_portal(), window=window)

    assert result.jobs


# --- Pagination -------------------------------------------------------------


async def test_a_second_page_is_fetched_and_does_not_overlap(stub_search) -> None:
    calls = stub_search()

    result = await _search(_portal(), limit=40)

    skips = [call["params"]["skip"] for call in calls]
    assert skips[:2] == [0, PAGE_SIZE]
    assert len({job.external_id for job in result.jobs}) == len(result.jobs)
    assert len(result.jobs) == 40


async def test_paging_stops_at_the_page_cap(stub_search) -> None:
    """The fixture's pages all report more, so only the cap can stop it."""
    calls = stub_search()

    await _search(_portal(), limit=1000)

    assert len(calls) == MAX_PAGES


async def test_paging_stops_when_the_endpoint_reports_no_more(stub_search) -> None:
    def _one_page(_url, params):
        if int(params["skip"]) == 0:
            return {"jobs": [{"id": "a", "title": "Financial Analyst",
                              "url": "https://jobs.example.com/a"}],
                    "hasMore": False}
        raise AssertionError("must not ask for a page the endpoint says is absent")

    calls = stub_search(payload_for=_one_page)

    await _search(_portal(), limit=100)

    assert len(calls) == 1


async def test_paging_stops_on_an_empty_page(stub_search) -> None:
    def _empty_second(_url, params):
        if int(params["skip"]) == 0:
            return {"jobs": [{"id": "a", "title": "Financial Analyst",
                              "url": "https://jobs.example.com/a"}],
                    "hasMore": True}
        return {"jobs": [], "hasMore": True}

    calls = stub_search(payload_for=_empty_second)

    result = await _search(_portal(), limit=100)

    assert len(calls) == 2
    assert len(result.jobs) == 1


async def test_the_limit_is_respected(stub_search) -> None:
    stub_search()

    result = await _search(_portal(), limit=5)

    assert len(result.jobs) == 5


async def test_a_listing_is_returned_once_per_pass(stub_search) -> None:
    stub_search()

    result = await _search(_portal(), queries=(QUERY, QUERY))

    identities = [job.external_id for job in result.jobs]
    assert len(identities) == len(set(identities))


# --- Parsing ----------------------------------------------------------------


async def test_a_listing_maps_onto_the_shared_contract(stub_search) -> None:
    stub_search()

    result = await _search(_portal(), limit=1)
    job = result.jobs[0]

    assert job.source == "scalejobs"
    assert job.external_id
    assert job.title
    assert job.company
    assert job.source_url.startswith("https://")
    assert job.application_url == job.source_url
    assert job.posted_at is not None and job.posted_at.tzinfo is not None
    assert job.raw_payload


async def test_every_captured_listing_links_to_the_employers_ats(
    records: list[dict[str, Any]],
) -> None:
    """The link is where the application is submitted, not a portal page."""
    adapter = _portal()

    for record in records:
        job = adapter._to_raw_job(record)
        if job is None:
            continue
        assert "scale.jobs" not in job.source_url
        assert "linkedin.com" not in job.source_url


def test_a_record_without_an_identity_is_skipped() -> None:
    adapter = _portal()

    assert adapter._to_raw_job(
        {"title": "Financial Analyst", "url": "https://jobs.example.com/a"}
    ) is None


def test_a_record_without_a_title_is_skipped() -> None:
    adapter = _portal()

    assert adapter._to_raw_job({"id": "a", "url": "https://x.example.com/a"}) is None


def test_a_record_without_a_link_is_skipped() -> None:
    """A listing the user cannot open is not a result."""
    adapter = _portal()

    assert adapter._to_raw_job({"id": "a", "title": "Financial Analyst"}) is None


def test_external_id_falls_back_to_the_alternate_field() -> None:
    adapter = _portal()

    job = adapter._to_raw_job(
        {
            "externalId": "ext:abc",
            "title": "Financial Analyst",
            "url": "https://jobs.example.com/a",
        }
    )

    assert job is not None
    assert job.external_id == "ext:abc"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-07-09T10:33:50.511Z", datetime(2026, 7, 9, 10, 33, 50, 511000, tzinfo=UTC)),
        ("2026-07-09T10:33:50+00:00", datetime(2026, 7, 9, 10, 33, 50, tzinfo=UTC)),
        ("", None),
        ("   ", None),
        ("not a date", None),
        (None, None),
        (1752057230, None),
    ],
)
def test_posted_at_is_parsed_or_left_empty(value, expected) -> None:
    assert ScaleJobsPortal._posted_at(value) == expected


def test_a_naive_timestamp_is_treated_as_utc() -> None:
    parsed = ScaleJobsPortal._posted_at("2026-07-09T10:33:50")

    assert parsed is not None
    assert parsed.tzinfo is UTC


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        # The ISO country code is resolved to a name, and the copy of it that
        # trails the city and state strings is dropped.
        ({"jobLocations": [{"city": "Evansville, Indiana, US",
                            "state": "Indiana, US", "country": "US"}]},
         "Evansville, Indiana, United States"),
        ({"jobLocations": [{"city": "Dublin", "country": "IE"}]},
         "Dublin, Ireland"),
        ({"jobLocations": [{"country": "RO"}]}, "Romania"),
        ({"jobLocations": [{"city": "Cascais, Lisbon, PT",
                            "state": "Lisbon, PT", "country": "PT"}]},
         "Cascais, Lisbon, Portugal"),
        # An unknown code is passed through rather than invented into a name.
        ({"jobLocations": [{"country": "ZZ"}]}, "ZZ"),
        ({"jobLocations": [], "locationsAltRaw": ["Romania or Europe"]},
         "Romania or Europe"),
        ({"jobLocations": None, "locationsRaw": "Austin, TX"}, "Austin, TX"),
        ({"isRemote": True}, "Remote"),
        ({}, None),
        ({"jobLocations": ["not a dict"]}, None),
        ({"jobLocations": [{}]}, None),
    ],
)
def test_location_reads_whichever_field_carries_one(record, expected) -> None:
    assert ScaleJobsPortal._location(record) == expected


@pytest.mark.parametrize(
    ("code", "country"),
    [("MT", "Malta"), ("CA", "Canada"), ("IN", "India"), ("DE", "Germany"),
     ("MA", "Morocco"), ("PA", "Panama"), ("ME", "Montenegro")],
)
def test_a_country_code_that_is_also_a_state_is_spelled_out(
    code: str, country: str
) -> None:
    """This endpoint really does publish ``{"country": "MT"}`` for Malta.

    Left as a bare code it would reach the US filter looking like Montana, so
    it is resolved here, where the field is known to be a country.
    """
    from app.processing.us_locations import classify_us_location

    location = ScaleJobsPortal._location({"jobLocations": [{"country": code}]})

    assert location == country
    assert not classify_us_location(location).is_us


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"employmentType": "Full Time"}, "Full Time"),
        ({"aiEmploymentType": "Contract"}, "Contract"),
        ({"employmentType": "", "aiEmploymentType": "Part Time"}, "Part Time"),
        ({"employmentType": ["Full Time", "Contract"]}, "Full Time"),
        ({}, None),
        ({"employmentType": None}, None),
    ],
)
def test_employment_type_reads_either_field(record, expected) -> None:
    assert ScaleJobsPortal._employment_type(record) == expected


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"aiSalaryAnnualMin": 69680, "aiSalaryAnnualMax": 104520,
          "aiSalaryCurrency": "USD"}, "USD 69,680 - 104,520 per year"),
        ({"aiSalaryAnnualMin": 80000, "aiSalaryCurrency": "USD"},
         "USD 80,000 per year"),
        ({"aiSalaryAnnualMax": 90000, "aiSalaryCurrency": "EUR"},
         "EUR 90,000 per year"),
        ({"aiSalaryAnnualMin": 0, "aiSalaryAnnualMax": 0}, None),
        ({}, None),
        ({"aiSalaryAnnualMin": True}, None),
    ],
)
def test_salary_is_rendered_only_when_published(record, expected) -> None:
    assert ScaleJobsPortal._salary(record) == expected


# --- Descriptions -----------------------------------------------------------


def test_the_description_starts_from_the_publishers_own_text() -> None:
    summary = "A" * 120

    text = ScaleJobsPortal._description({"descriptionText": summary})

    assert text is not None
    assert text.startswith(summary)


def test_a_missing_summary_yields_no_description() -> None:
    """Downstream rejects an unscoreable posting; that is the right outcome."""
    assert ScaleJobsPortal._description({}) is None
    assert ScaleJobsPortal._description({"descriptionText": None}) is None


def test_a_stub_summary_is_not_padded_past_the_floor() -> None:
    """Structured extras must not rescue a listing that has no real text.

    The scoreability floor exists to reject postings that cannot be judged;
    composing around it would defeat it.
    """
    record = {
        "descriptionText": "Short.",
        "aiEmploymentType": "Full Time",
        "aiWorkArrangement": "Remote",
        "aiExperienceLevel": "Mid Level",
        "aiMinExperience": 3,
        "aiSalaryAnnualMin": 80000,
        "aiSalaryAnnualMax": 120000,
        "aiSalaryCurrency": "USD",
        "aiVisaSponsorship": True,
        "jobLocations": [{"city": "Austin", "country": "US"}],
    }

    assert ScaleJobsPortal._description(record) is None


def test_the_description_adds_facts_the_summary_does_not_state() -> None:
    record = {
        "descriptionText": "B" * 120,
        "aiEmploymentType": "Full Time",
        "aiWorkArrangement": "Hybrid",
        "aiExperienceLevel": "Mid Level",
        "aiMinExperience": 3,
        "aiSalaryAnnualMin": 80000,
        "aiSalaryAnnualMax": 120000,
        "aiSalaryCurrency": "USD",
        "aiVisaSponsorship": True,
        "jobLocations": [{"city": "Austin", "country": "US"}],
    }

    text = ScaleJobsPortal._description(record)

    assert text is not None
    assert "Employment type: Full Time" in text
    assert "Work arrangement: Hybrid" in text
    assert "Experience level: Mid Level" in text
    assert "Minimum experience: 3 years" in text
    assert "Compensation: USD 80,000 - 120,000 per year" in text
    assert "Visa sponsorship: stated as available" in text
    assert "Location: Austin, United States" in text


def test_sponsorship_is_only_stated_when_it_is_offered() -> None:
    base = {"descriptionText": "C" * 120}

    assert "Visa sponsorship" not in (
        ScaleJobsPortal._description({**base, "aiVisaSponsorship": False}) or ""
    )
    assert "Visa sponsorship" not in (ScaleJobsPortal._description(base) or "")


async def test_captured_descriptions_clear_the_scoreable_floor(
    records: list[dict[str, Any]],
) -> None:
    """Most real listings carry enough text to be judged; some do not."""
    adapter = _portal()
    described = [
        job
        for record in records
        if (job := adapter._to_raw_job(record)) is not None and job.description
    ]

    assert len(described) >= len(records) - 2
    assert all(len(job.description or "") >= 200 for job in described)


async def test_fetch_description_returns_what_was_already_collected() -> None:
    """There is no per-job endpoint, so there is nothing further to fetch."""
    stub_search_called = False
    adapter = _portal()
    job = adapter._to_raw_job(
        {
            "id": "a",
            "title": "Financial Analyst",
            "url": "https://jobs.example.com/a",
            "descriptionText": "D" * 120,
        }
    )

    assert job is not None
    assert await adapter.fetch_description(job) == job.description
    assert stub_search_called is False


# --- Malformed responses ----------------------------------------------------


async def test_a_response_without_a_jobs_key_is_a_parse_error(stub_search) -> None:
    """An empty search returns an empty list; a missing key means a shape change."""
    stub_search(payload_for=lambda _url, _params: {"hasMore": False})

    with pytest.raises(PortalParseError):
        await _search(_portal())


async def test_a_response_whose_jobs_are_not_a_list_is_a_parse_error(
    stub_search,
) -> None:
    stub_search(payload_for=lambda _url, _params: {"jobs": {"a": 1}})

    with pytest.raises(PortalParseError):
        await _search(_portal())


async def test_an_empty_result_set_is_not_an_error(stub_search) -> None:
    stub_search(payload_for=lambda _url, _params: {"jobs": [], "hasMore": False})

    result = await _search(_portal())

    assert result.jobs == []


async def test_non_dict_entries_are_ignored(stub_search) -> None:
    stub_search(
        payload_for=lambda _url, _params: {
            "jobs": [
                "not a record",
                None,
                {"id": "a", "title": "Financial Analyst",
                 "url": "https://jobs.example.com/a"},
            ],
            "hasMore": False,
        }
    )

    result = await _search(_portal())

    assert len(result.jobs) == 1


async def test_unusable_records_do_not_lose_the_usable_ones(stub_search) -> None:
    stub_search(
        payload_for=lambda _url, _params: {
            "jobs": [
                {"title": "no id", "url": "https://x.example.com/1"},
                {"id": "b", "title": "Financial Analyst",
                 "url": "https://jobs.example.com/b"},
                {"id": "c", "title": "", "url": "https://x.example.com/3"},
            ],
            "hasMore": False,
        }
    )

    result = await _search(_portal())

    assert [job.external_id for job in result.jobs] == ["b"]


# --- The publisher's own outage ---------------------------------------------
#
# On 2026-08-25 `source=live` - the fresh inventory this adapter reads - began
# answering:
#
#     500 {"error":"Request failed with status code 407", ...}
#
# That 407 is *their* upstream proxy refusing *them*; their own `/health` and
# `/api/stats` were failing too, and their hourly sync cron was stopped. The
# request this adapter sends still matches the endpoint's published contract,
# so there is nothing here to correct - what matters is that the outage is
# reported as an outage.


async def test_a_five_hundred_is_never_read_as_an_empty_job_list(
    stub_search,
) -> None:
    """The failure mode that would quietly shrink the job pool.

    A portal that errors must not look like a portal with nothing to offer.
    The adapter raises, the collector marks the leg failed, and the run says
    so - rather than reporting a successful search that found no jobs.
    """

    def _raise(_url, _params):
        raise PortalError("scalejobs returned HTTP 500.", portal="scalejobs")

    stub_search(payload_for=_raise)

    with pytest.raises(PortalError) as exc_info:
        await _portal().search(
            PortalSearchRequest(
                queries=(QUERY,), window=FreshnessWindow.LAST_7D, limit=20
            )
        )

    assert "500" in exc_info.value.message


async def test_the_live_inventory_is_still_what_is_requested(stub_search) -> None:
    """The adapter must not quietly fall back to the stale database path.

    Omitting `source=live` reaches the publisher's own AlloyDB copy, which
    answers 200 - and whose newest listing, measured on 2026-08-25, was 41 days
    old. Every job in it falls outside the locked 30-day window, so that path
    would return a successful search containing nothing eligible: an outage
    dressed up as an empty result, which is exactly what must not happen.
    """
    calls = stub_search()

    await _portal().search(
        PortalSearchRequest(
            queries=(QUERY,), window=FreshnessWindow.LAST_7D, limit=5
        )
    )

    assert calls, "the adapter must actually issue a request"
    for call in calls:
        assert call["params"]["source"] == "live"


async def test_an_error_on_a_later_page_still_surfaces(stub_search) -> None:
    """A partial page must not hide a failure behind it."""

    def _pages(_url, params):
        if int(params.get("skip", 0)) == 0:
            return {
                "jobs": [
                    {
                        "id": "a",
                        "title": "Financial Analyst",
                        "url": "https://boards.greenhouse.io/acme/jobs/1",
                        "descriptionText": "x" * 300,
                    }
                ],
                "hasMore": True,
            }
        raise PortalError("scalejobs returned HTTP 500.", portal="scalejobs")

    stub_search(payload_for=_pages)

    with pytest.raises(PortalError):
        await _portal(max_pages=2).search(
            PortalSearchRequest(
                queries=(QUERY,), window=FreshnessWindow.LAST_30D, limit=20
            )
        )


# --- Failure isolation ------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        PortalBlockedError("refused", portal="scalejobs"),
        PortalRateLimitedError("slow down", portal="scalejobs"),
        PortalTimeoutError("too slow", portal="scalejobs"),
        PortalParseError("bad shape", portal="scalejobs"),
        PortalError("generic", portal="scalejobs"),
    ],
)
async def test_a_transport_failure_surfaces_as_a_portal_error(
    stub_search, error: PortalError
) -> None:
    """The taxonomy is what lets a run record the failure and continue."""

    def _raise(_url, _params):
        raise error

    stub_search(payload_for=_raise)

    with pytest.raises(type(error)):
        await _search(_portal())


async def test_a_failure_on_the_second_query_does_not_hide_the_first(
    stub_search,
) -> None:
    """One query failing must not lose a pass that already collected."""

    def _fail_second(_url, params):
        if params["jobTitles"] != QUERY:
            raise PortalError("second query down", portal="scalejobs")
        return {"jobs": [{"id": "a", "title": "Financial Analyst",
                          "url": "https://jobs.example.com/a"}],
                "hasMore": False}

    stub_search(payload_for=_fail_second)

    # The failure is raised so the run records it against this portal - the
    # orchestrator is what keeps the other portals running.
    with pytest.raises(PortalError):
        await _search(_portal(), queries=(QUERY, "Senior Financial Analyst"))


# --- Policy -----------------------------------------------------------------


def test_operational_policy_comes_from_the_portal_config() -> None:
    adapter = _portal(
        timeout_seconds=12.5, max_retries=4, rate_limit_per_minute=10
    )

    assert adapter._http.policy.timeout_seconds == 12.5
    assert adapter._http.policy.max_retries == 4
    assert adapter._http.policy.rate_limit_per_minute == 10


def test_unknown_options_are_ignored() -> None:
    adapter = _portal(nonsense=True)

    assert adapter._http.policy.timeout_seconds == 30.0


async def test_health_check_probes_the_endpoint_actually_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host's /health route reports on the publisher's own database.

    It currently answers 500 after thirty seconds, which says nothing about
    whether search works for this client.
    """
    probed: list[str] = []

    async def _head_ok(self, url):  # noqa: ANN001
        probed.append(url)
        return True

    monkeypatch.setattr("app.portals.http.PortalHttpClient.head_ok", _head_ok)

    health = await _portal().health_check()

    assert health.is_available is True
    assert probed == [SEARCH_URL]


async def test_an_unreachable_endpoint_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _head_ok(self, url):  # noqa: ANN001
        return False

    monkeypatch.setattr("app.portals.http.PortalHttpClient.head_ok", _head_ok)

    health = await _portal().health_check()

    assert health.is_available is False
    assert health.detail
