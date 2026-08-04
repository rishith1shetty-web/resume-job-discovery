"""URL validation, normalisation, classification and preferred-URL selection.

The rule that matters most here is negative: a URL is never promoted to
"employer direct" on a hunch. ``JOB_URL_PREFERENCE`` ranks employer links above
everything else, so a wrong guess would push a speculative link ahead of a
portal link that actually works.

Nothing in this module or the code it tests opens a URL, follows a redirect or
submits anything.
"""

from __future__ import annotations

import pytest

from app.core.constants import JOB_URL_PREFERENCE, JobUrlKind
from app.processing.urls import (
    MAX_URL_LENGTH,
    canonical_url,
    classify_url,
    is_valid_url,
    normalize_and_classify,
    normalize_url,
)

# --- Validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://jobright.ai/jobs/info/abc123",
        "http://careers.example.com/job/1",
        "https://example.com:8443/job/1",
    ],
)
def test_http_and_https_urls_are_accepted(url: str) -> None:
    assert is_valid_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "javascript:void(0)",
        "data:text/html,<h1>job</h1>",
        "mailto:jobs@example.com",
        "ftp://example.com/job",
        "/jobs/relative-path",
        "not a url",
        "https://",
        "",
        None,
    ],
)
def test_anything_that_is_not_an_http_url_is_rejected(url: str | None) -> None:
    assert is_valid_url(url) is False
    assert normalize_url(url) is None
    assert normalize_and_classify(url) is None


def test_a_url_too_long_to_store_is_rejected() -> None:
    # Truncating would produce a link that silently 404s.
    assert is_valid_url("https://example.com/" + "x" * MAX_URL_LENGTH) is False


# --- Normalisation ----------------------------------------------------------


def test_fragments_are_removed() -> None:
    assert normalize_url("https://example.com/job/1#apply") == (
        "https://example.com/job/1"
    )


def test_scheme_and_host_are_lowercased_but_the_path_is_not() -> None:
    # Paths are case-sensitive on many servers; changing one breaks the link.
    assert normalize_url("HTTPS://Example.COM/Job/Senior-Analyst") == (
        "https://example.com/Job/Senior-Analyst"
    )


def test_default_ports_are_dropped_and_others_kept() -> None:
    assert normalize_url("https://example.com:443/job/1") == "https://example.com/job/1"
    assert normalize_url("http://example.com:80/job/1") == "http://example.com/job/1"
    assert normalize_url("https://example.com:8443/job/1") == (
        "https://example.com:8443/job/1"
    )


@pytest.mark.parametrize(
    "tracking",
    [
        "utm_source=jobright",
        "utm_campaign=spring&utm_medium=email",
        "gclid=abc123",
        "fbclid=xyz",
        "trk=public_jobs",
        "refId=deadbeef",
        "gh_src=abcdef",
        "lever-origin=applied",
    ],
)
def test_tracking_parameters_are_stripped(tracking: str) -> None:
    assert normalize_url(f"https://example.com/job/1?{tracking}") == (
        "https://example.com/job/1"
    )


@pytest.mark.parametrize(
    "identity",
    ["gh_jid=4123456", "jobId=998", "currentJobId=3901234567", "jk=abc123"],
)
def test_parameters_that_identify_the_job_are_kept(identity: str) -> None:
    # Greenhouse resolves the vacancy from gh_jid; dropping it breaks the link.
    assert identity in (normalize_url(f"https://example.com/job?{identity}") or "")


def test_tracking_is_stripped_without_losing_the_job_id() -> None:
    normalized = normalize_url(
        "https://boards.greenhouse.io/acme/jobs/4123456?gh_src=abc&gh_jid=4123456"
    )

    assert normalized == "https://boards.greenhouse.io/acme/jobs/4123456?gh_jid=4123456"


def test_an_unrecognised_parameter_is_kept() -> None:
    # An unknown parameter is more likely to identify the job than to be
    # analytics, and a link that no longer opens is worse than an untidy one.
    assert "sourceType=custom" in (
        normalize_url("https://example.com/job/1?sourceType=custom") or ""
    )


def test_trailing_slashes_are_cosmetic_except_at_the_root() -> None:
    assert normalize_url("https://example.com/job/1/") == "https://example.com/job/1"
    assert normalize_url("https://example.com") == "https://example.com/"


def test_the_original_url_is_never_mutated_by_normalising_it() -> None:
    original = "https://jobright.ai/jobs/info/abc?utm_source=1014"

    normalize_url(original)

    assert original == "https://jobright.ai/jobs/info/abc?utm_source=1014"


# --- Canonical form ---------------------------------------------------------


def test_canonical_form_ignores_scheme_www_and_parameter_order() -> None:
    left = canonical_url("http://www.example.com/job/1?b=2&a=1")
    right = canonical_url("https://example.com/job/1?a=1&b=2")

    assert left == right


def test_canonical_form_still_separates_different_jobs() -> None:
    assert canonical_url("https://example.com/job/1") != canonical_url(
        "https://example.com/job/2"
    )


# --- Classification ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/acme/jobs/4123456",
        "https://jobs.lever.co/contoso/8f2f-11ee",
        "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/R-1",
        "https://acme.icims.com/jobs/1234/analyst/job",
        "https://acme.smartrecruiters.com/Acme/74400001",
    ],
)
def test_applicant_tracking_systems_are_recognised(url: str) -> None:
    assert classify_url(url) is JobUrlKind.ATS


def test_a_portals_own_listing_is_recognised_as_such() -> None:
    assert (
        classify_url("https://jobright.ai/jobs/info/abc", portal_slug="jobright")
        is JobUrlKind.PORTAL_LISTING
    )


def test_another_boards_link_is_an_aggregator_hop() -> None:
    assert (
        classify_url("https://www.linkedin.com/jobs/view/1", portal_slug="jobright")
        is JobUrlKind.AGGREGATOR_REDIRECT
    )


def test_employer_direct_is_only_claimed_when_the_domain_proves_it() -> None:
    assert (
        classify_url("https://careers.berkadia.com/job/R123", company="Berkadia")
        is JobUrlKind.EMPLOYER_CAREER
    )


@pytest.mark.parametrize(
    ("url", "company"),
    [
        # A careers site parked on the company name plus a word. Found via
        # Hiring Cafe, which links employer-direct far more often than a board
        # does, so this shape had not been exercised before.
        ("https://jobs.compassgroupcareers.com/job/123", "Compass Group"),
        ("https://berkadiacareers.com/job/1", "Berkadia"),
        ("https://senecaholdings.com/careers/1", "Seneca Holdings"),
    ],
)
def test_a_company_named_careers_domain_is_employer_direct(
    url: str, company: str
) -> None:
    assert classify_url(url, company=company) is JobUrlKind.EMPLOYER_CAREER


@pytest.mark.parametrize(
    ("url", "company"),
    [
        # A short company key must not claim every host that starts with it.
        ("https://bankjobs.com/1", "Bank"),
        ("https://acmecorp.com/1", "AB"),
        # Sharing a prefix is not the same as being the employer.
        ("https://compassionatecare.com/1", "Compass"),
    ],
)
def test_a_prefix_alone_does_not_prove_the_employer(
    url: str, company: str
) -> None:
    assert classify_url(url, company=company) is not JobUrlKind.EMPLOYER_CAREER


@pytest.mark.parametrize(
    ("url", "company"),
    [
        # Nothing ties this host to the employer.
        ("https://apply.talentplatform.example/job/1", "Berkadia"),
        # A careers-looking path is not evidence of anything.
        ("https://third-party.example/careers/job/1", "Berkadia"),
        # No employer to compare against.
        ("https://careers.berkadia.com/job/R123", None),
        # Too short a name to be distinctive.
        ("https://abc.com/job/1", "ABC"),
    ],
)
def test_an_unverified_destination_stays_unknown(url: str, company: str | None) -> None:
    # UNKNOWN ranks last, so an unproven link never displaces a working one.
    assert classify_url(url, company=company) is JobUrlKind.UNKNOWN


def test_preference_order_matches_the_specification() -> None:
    # CLAUDE.md section 9: employer career page, ATS, portal listing, then
    # aggregator redirect.
    ranks = JOB_URL_PREFERENCE

    assert ranks[JobUrlKind.EMPLOYER_CAREER] < ranks[JobUrlKind.ATS]
    assert ranks[JobUrlKind.ATS] < ranks[JobUrlKind.PORTAL_LISTING]
    assert ranks[JobUrlKind.PORTAL_LISTING] < ranks[JobUrlKind.AGGREGATOR_REDIRECT]
    assert ranks[JobUrlKind.AGGREGATOR_REDIRECT] < ranks[JobUrlKind.UNKNOWN]


def test_normalise_and_classify_reports_rank_for_selection() -> None:
    ats = normalize_and_classify("https://boards.greenhouse.io/acme/jobs/1")
    listing = normalize_and_classify(
        "https://jobright.ai/jobs/info/abc", portal_slug="jobright"
    )

    assert ats is not None and listing is not None
    assert ats.preference_rank < listing.preference_rank
