"""LinkedIn adapter: access refusal, parsing, and the exclusion rule.

No test here contacts LinkedIn. Their robots.txt disallows automated access for
this client, so the fixture is synthetic and every network call is stubbed.
That is also what the first group of tests is checking: that the adapter
refuses to make the call at all unless an operator has asserted authorisation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.constants import FreshnessWindow
from app.portals.adapters.linkedin import (
    ACCESS_NOTICE,
    AUTHORIZATION_OPTION,
    LinkedInPortal,
    external_apply_url,
    is_in_platform_application,
)
from app.portals.base import PortalSearchRequest
from app.portals.exceptions import PortalBlockedError

FIXTURE = (
    Path(__file__).parent / "fixtures" / "portals" / "linkedin_search_results.html"
)


@pytest.fixture
def fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def authorized() -> LinkedInPortal:
    """An adapter an operator has declared authorised to collect."""
    return LinkedInPortal(options={AUTHORIZATION_OPTION: True})


@pytest.fixture
def unauthorized() -> LinkedInPortal:
    return LinkedInPortal(options={})


# --- Access refusal ---------------------------------------------------------


async def test_collection_is_refused_without_explicit_authorization(
    unauthorized: LinkedInPortal,
) -> None:
    request = PortalSearchRequest(
        queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=5
    )

    with pytest.raises(PortalBlockedError) as exc_info:
        await unauthorized.search(request)

    assert exc_info.value.portal == "linkedin"
    assert "robots.txt" in exc_info.value.message


async def test_refusal_happens_before_any_network_call(
    unauthorized: LinkedInPortal,
) -> None:
    """The point of the gate: no request is made, not merely discarded."""

    async def _explode(*_args, **_kwargs) -> str:
        raise AssertionError("no request may be made without authorization")

    unauthorized._http.get_text = _explode  # type: ignore[attr-defined]

    with pytest.raises(PortalBlockedError):
        await unauthorized.search(
            PortalSearchRequest(
                queries=("Financial Analyst",),
                window=FreshnessWindow.LAST_7D,
                limit=5,
            )
        )


async def test_health_check_reports_the_access_position_without_probing(
    unauthorized: LinkedInPortal,
) -> None:
    async def _explode(_url: str) -> bool:
        raise AssertionError("must not probe a portal that has refused access")

    unauthorized._http.head_ok = _explode  # type: ignore[attr-defined]

    health = await unauthorized.health_check()

    assert health.is_available is False
    assert health.detail == ACCESS_NOTICE
    assert "whitelist-crawl@linkedin.com" in health.detail


async def test_url_validation_is_declined_when_unauthorized(
    unauthorized: LinkedInPortal,
) -> None:
    assert await unauthorized.validate_url("https://www.linkedin.com/jobs/view/1") is False


def test_the_access_notice_does_not_suggest_a_workaround() -> None:
    # The notice must point at obtaining permission, never at evading the block.
    lowered = ACCESS_NOTICE.lower()

    assert "permission" in lowered
    for evasion in ("bypass", "circumvent", "proxy", "spoof", "captcha", "stealth"):
        if evasion in lowered:
            # The only permitted mention is an explicit denial.
            assert "no restriction is bypassed" in lowered


# --- The exclusion rule -----------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [
        {"applyMethod": "ComplexOnsiteApply"},
        {"applyMethod": "SimpleOnsiteApply"},
        {"applyLabel": "Easy Apply"},
        {"applyType": "easy_apply"},
        {"easyApply": True},
        {"isEasyApply": True},
    ],
)
def test_in_platform_applications_are_detected(record: dict) -> None:
    assert is_in_platform_application(record) is True


@pytest.mark.parametrize(
    "record",
    [
        {"applyMethod": "OffsiteApply"},
        {"companyApplyUrl": "https://careers.example.com/apply/1"},
        {},
    ],
)
def test_external_applications_are_not_flagged(record: dict) -> None:
    assert is_in_platform_application(record) is False


def test_external_apply_url_is_read_from_any_known_field() -> None:
    assert external_apply_url(
        {"companyApplyUrl": "https://boards.greenhouse.io/x/jobs/1"}
    ) == "https://boards.greenhouse.io/x/jobs/1"
    assert external_apply_url({"applyUrl": "https://wd5.myworkdayjobs.com/x"}) == (
        "https://wd5.myworkdayjobs.com/x"
    )
    assert external_apply_url({"companyApplyUrl": "javascript:void(0)"}) is None
    assert external_apply_url({}) is None


def test_parsing_excludes_in_platform_listings(
    authorized: LinkedInPortal, fixture_html: str
) -> None:
    records, excluded = authorized.parse_search_results(fixture_html)

    titles = [record["title"] for record in records]
    # Five cards: two external (kept), one Easy Apply with no link, one with no
    # link at all, and one Easy Apply that *does* carry an external URL.
    assert "FP&A Analyst" not in titles
    assert excluded == 3
    assert len(records) == 2


def test_an_easy_apply_listing_is_excluded_even_with_an_external_url(
    authorized: LinkedInPortal, fixture_html: str
) -> None:
    """The label alone must be enough to exclude.

    Without this case the label check is invisible, because every other Easy
    Apply card is already caught by the "no external link" rule — which is
    exactly how a real matching bug once slipped through.
    """
    records, _ = authorized.parse_search_results(fixture_html)

    assert "Junior Financial Analyst" not in [record["title"] for record in records]


def test_every_returned_listing_has_an_external_apply_link(
    authorized: LinkedInPortal, fixture_html: str
) -> None:
    # The product's rule: a job the user cannot open and apply to manually is
    # not a usable result.
    records, _ = authorized.parse_search_results(fixture_html)

    assert records
    for record in records:
        url = external_apply_url(record)
        assert url and url.startswith("http")
        assert "linkedin.com" not in url


# --- Mapping ----------------------------------------------------------------


async def test_search_maps_onto_the_raw_job_contract(
    authorized: LinkedInPortal, fixture_html: str
) -> None:
    async def _fetch(*_args, **_kwargs) -> str:
        return fixture_html

    authorized._http.get_text = _fetch  # type: ignore[attr-defined]

    result = await authorized.search(
        PortalSearchRequest(
            queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=10
        )
    )

    assert len(result.jobs) == 2
    job = result.jobs[0]
    assert job.source == "linkedin"
    assert job.title == "Financial Analyst"
    assert job.company == "Northwind Trading"
    assert job.location == "New York, NY"
    assert job.external_id == "3901234567"
    assert job.source_url.startswith("https://www.linkedin.com/jobs/view/")
    # External redirects are exactly what is wanted.
    assert job.application_url == "https://boards.greenhouse.io/example/jobs/4455661"
    assert job.posted_at is not None
    assert job.raw_payload


async def test_search_respects_the_limit(
    authorized: LinkedInPortal, fixture_html: str
) -> None:
    async def _fetch(*_args, **_kwargs) -> str:
        return fixture_html

    authorized._http.get_text = _fetch  # type: ignore[attr-defined]

    result = await authorized.search(
        PortalSearchRequest(
            queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=1
        )
    )

    assert len(result.jobs) == 1


def test_malformed_markup_yields_nothing_rather_than_raising(
    authorized: LinkedInPortal,
) -> None:
    records, excluded = authorized.parse_search_results(
        "<html><body><li>not a job card</li></body></html>"
    )

    assert records == []
    assert excluded == 0


def test_a_card_without_a_title_is_dropped(authorized: LinkedInPortal) -> None:
    card = (
        '<li><div class="base-card job-search-card" '
        'data-external-apply-url="https://careers.example.com/1">'
        '<a class="base-card__full-link" '
        'href="https://www.linkedin.com/jobs/view/x-3900000001"></a>'
        "</div></li>"
    )
    records, _ = authorized.parse_search_results(card)

    assert all(authorized._to_raw_job(record) is None for record in records)


def test_freshness_windows_map_to_the_portals_own_parameter(
    authorized: LinkedInPortal,
) -> None:
    request = PortalSearchRequest(
        queries=("x",), window=FreshnessWindow.LAST_24H, limit=1
    )
    params = authorized._search_params("Financial Analyst", request)

    assert params["f_TPR"] == "r86400"
    assert params["keywords"] == "Financial Analyst"


def test_the_oldest_window_applies_no_date_filter(
    authorized: LinkedInPortal,
) -> None:
    # OLDER_ACTIVE means "stop constraining by date", not "filter by zero".
    request = PortalSearchRequest(
        queries=("x",), window=FreshnessWindow.OLDER_ACTIVE, limit=1
    )

    assert "f_TPR" not in authorized._search_params("x", request)
