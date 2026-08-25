"""Career pages are verified by opening them, not by reading their spelling.

Every test here hands the validator a body a real site could serve and checks
the verdict. That is the whole point of the change these tests cover: the
previous rule accepted a URL on its shape, and measured against real collected
postings it shipped six dead or gated destinations out of twelve - a Company ID
login page, an ADP login redirect, two 404s, a 406 and an Oracle SSO redirect -
each announced to the user as an "official career page verified".

So the failure classes below are not hypothetical. They are the ones that were
actually returned, written out as the markup that produced them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.careers.base import CareerPage, CareerPageDiscovery
from app.careers.candidates import career_url_candidates
from app.careers.evidence import access_verdict, read_page
from app.careers.fetching import address_is_safe
from app.careers.robots import RobotsPolicy
from app.careers.validation import validate_candidate, validate_company
from app.careers.verification import admit_career_url
from app.core.constants import CareerPageStatus, JobUrlKind
from app.services.career_page_verification import verify_career_pages
from tests.fake_career_pages import (
    FakeCareerFetcher,
    Page,
    careers_page,
    company_id_page,
    dead_but_two_hundred_page,
    gone_page,
    login_page,
    marketing_page,
    not_found_page,
    shell_page,
    vendor_front_door,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


async def verdict(
    url: str, pages: dict[str, Page], *, company: str, kind=JobUrlKind.EMPLOYER_CAREER
):
    """Validate one URL against a scripted set of pages."""
    fetcher = FakeCareerFetcher(pages=pages)
    return await validate_candidate(
        url, company=company, kind=kind, fetcher=fetcher, now=NOW
    )


# --- Accepted: pages a person can actually use ------------------------------


@pytest.mark.asyncio
async def test_an_official_employer_careers_page_is_accepted() -> None:
    result = await verdict(
        "https://careers.northwind-analytics.com/",
        {"https://careers.northwind-analytics.com/": careers_page("Northwind Analytics")},
        company="Northwind Analytics",
    )

    assert result.status is CareerPageStatus.VERIFIED
    assert result.url == "https://careers.northwind-analytics.com/"
    assert "publicly browsable" in result.reason


@pytest.mark.asyncio
async def test_an_employer_job_search_portal_with_current_jobs_is_accepted() -> None:
    """Requirement 7 and 8: the openings are visible and reachable."""
    result = await verdict(
        "https://jobs.southgate-capital.com/en_US/externaljobs",
        {
            "https://jobs.southgate-capital.com/en_US/externaljobs": Page(
                body="""
                <html><head><title>Jobs at Southgate Capital</title></head>
                <body><h1>Job Search</h1>
                <input name="keyword" placeholder="Search jobs">
                <a href="/job/517144">Senior Financial Analyst</a>
                <a href="/job/517145">FP&A Analyst</a>
                <a href="/job/517146">Treasury Analyst</a>
                </body></html>
                """
            )
        },
        company="Southgate Capital",
    )

    assert result.status is CareerPageStatus.VERIFIED
    assert "3 job postings listed" in result.reason


@pytest.mark.parametrize(
    ("url", "company"),
    [
        ("https://boards.greenhouse.io/northwindanalytics", "Northwind Analytics"),
        ("https://jobs.lever.co/southgatecapital", "Southgate Capital"),
        ("https://ridgelineenergy.wd1.myworkdayjobs.com/ridgeline", "Ridgeline Energy"),
    ],
)
@pytest.mark.asyncio
async def test_a_company_specific_ats_board_is_accepted(url: str, company: str) -> None:
    """Requirement: employer-specific boards are exactly what we want."""
    result = await verdict(
        url, {url: careers_page(company)}, company=company, kind=JobUrlKind.ATS
    )

    assert result.status is CareerPageStatus.VERIFIED
    assert result.url == url


@pytest.mark.asyncio
async def test_a_browser_rendered_board_is_accepted_on_its_public_listings() -> None:
    """Workday-shaped boards serve an empty shell and load jobs in the browser.

    Judged on HTML alone they have no evidence and would be dropped, taking
    roughly a third of all employer application links with them. The board's
    own public listing endpoint answers the same question, so it is asked -
    and the acceptance rests on the openings it returns, never on the URL.
    """
    board = "https://ridgelineenergy.wd1.myworkdayjobs.com/ridgeline"
    fetcher = FakeCareerFetcher(
        pages={board: shell_page()},
        listings={
            "https://ridgelineenergy.wd1.myworkdayjobs.com"
            "/wday/cxs/ridgelineenergy/ridgeline/jobs": {
                "total": 41,
                "jobPostings": [
                    {"title": "Financial Analyst"},
                    {"title": "Senior FP&A Analyst"},
                ],
            }
        },
    )

    result = await validate_candidate(
        board,
        company="Ridgeline Energy",
        kind=JobUrlKind.ATS,
        fetcher=fetcher,
        now=NOW,
    )

    assert result.status is CareerPageStatus.VERIFIED
    assert result.listed_total == 41
    assert "41 current openings" in result.reason


@pytest.mark.asyncio
async def test_a_shell_with_no_public_listings_is_not_guessed_into_a_pass() -> None:
    """The same shell, without the listing evidence, is not accepted."""
    board = "https://opaque.wd1.myworkdayjobs.com/careers"
    result = await verdict(
        board, {board: shell_page()}, company="Opaque Holdings", kind=JobUrlKind.ATS
    )

    assert result.status is CareerPageStatus.UNVERIFIED
    assert result.url is None


# --- Rejected: the failure classes that were actually shipped ---------------


@pytest.mark.asyncio
async def test_a_company_id_page_is_rejected() -> None:
    """The bug that started this. It says "Careers" and asks for a Company ID."""
    url = "https://recruiting.paylocity.com/"
    result = await verdict(
        url, {url: company_id_page()}, company="AMBA", kind=JobUrlKind.ATS
    )

    assert result.status is CareerPageStatus.REJECTED
    assert "company or organisation ID" in result.reason
    assert result.url is None


@pytest.mark.asyncio
async def test_authentication_is_detected_before_careers_vocabulary() -> None:
    """The ordering requirement, asserted directly rather than assumed.

    The Company ID page carries an ``<h1>Careers</h1>`` and a nav with a "Jobs"
    link, so a rule that counted encouraging words first would pass it. The
    gate check sees it regardless of what else is on the page.
    """
    evidence = read_page(company_id_page().body)

    assert "Careers" in evidence.text and "Jobs" in evidence.text
    assert access_verdict(evidence).gated is True


@pytest.mark.asyncio
async def test_an_employee_login_page_is_rejected() -> None:
    url = "https://careers.acme.com/"
    result = await verdict(url, {url: login_page("Acme")}, company="Acme Robotics")

    assert result.status is CareerPageStatus.REJECTED
    assert "signing in" in result.reason


@pytest.mark.asyncio
async def test_a_sign_in_link_alone_does_not_reject_a_working_careers_page() -> None:
    """The other side of the login rule, and why it is not a keyword match.

    Real careers sites carry a "Sign in" link for returning applicants. An
    earlier draft rejected on that alone and threw away a working employer
    careers site, so a login wall only counts when the page shows no jobs.
    """
    url = "https://careers.northwind-analytics.com/"
    page = careers_page("Northwind Analytics")
    with_sign_in = Page(body=page.body.replace("<h1>", '<a href="/login">Sign in</a><h1>'))

    result = await verdict(url, {url: with_sign_in}, company="Northwind Analytics")

    assert result.status is CareerPageStatus.VERIFIED


@pytest.mark.parametrize("page", [not_found_page(), gone_page()])
@pytest.mark.asyncio
async def test_a_dead_page_is_rejected(page: Page) -> None:
    url = "https://careers.acme.com/jobs"
    result = await verdict(url, {url: page}, company="Acme Robotics")

    assert result.status is CareerPageStatus.REJECTED
    assert "not there" in result.reason


@pytest.mark.asyncio
async def test_a_two_hundred_that_says_the_job_is_gone_is_rejected() -> None:
    """``recruiting.paylocity.com/Recruiting/Jobs`` really answers like this."""
    url = "https://recruiting.paylocity.com/Recruiting/Jobs"
    result = await verdict(
        url,
        {url: dead_but_two_hundred_page()},
        company="AMBA",
        kind=JobUrlKind.ATS,
    )

    assert result.status is CareerPageStatus.REJECTED
    assert "no longer exists" in result.reason


@pytest.mark.asyncio
async def test_an_unreachable_url_is_unavailable_not_verified() -> None:
    result = await verdict(
        "https://careers.acme.com/", {}, company="Acme Robotics"
    )

    assert result.status is CareerPageStatus.UNAVAILABLE
    assert result.url is None


@pytest.mark.asyncio
async def test_a_generic_ats_homepage_is_rejected() -> None:
    url = "https://boards.greenhouse.io/"
    result = await verdict(
        url,
        {url: vendor_front_door("Greenhouse")},
        company="Northwind Analytics",
        kind=JobUrlKind.ATS,
    )

    assert result.status is CareerPageStatus.REJECTED
    assert "front page" in result.reason


@pytest.mark.asyncio
async def test_a_careers_marketing_page_with_no_jobs_is_not_verified() -> None:
    url = "https://www.acmerobotics.com/careers"
    result = await verdict(
        url, {url: marketing_page("Acme Robotics")}, company="Acme Robotics"
    )

    assert result.status is CareerPageStatus.UNVERIFIED
    assert result.url is None


@pytest.mark.asyncio
async def test_a_corporate_homepage_without_jobs_is_not_verified() -> None:
    url = "https://www.acmerobotics.com/"
    result = await verdict(
        url,
        {
            url: Page(
                body="<html><head><title>Acme Robotics</title></head><body>"
                "<h1>Acme Robotics</h1><p>We build robots for warehouses "
                "across North America, and we have done since 1998. Read "
                "about our products, our partners and our research.</p>"
                '<a href="/products">Products</a></body></html>'
            )
        },
        company="Acme Robotics",
    )

    assert result.status is CareerPageStatus.UNVERIFIED


@pytest.mark.asyncio
async def test_a_page_belonging_to_another_company_is_rejected() -> None:
    """Requirement: the destination must be the employer we think it is."""
    url = "https://boards.greenhouse.io/someoneelse"
    result = await verdict(
        url,
        {url: careers_page("Contoso Shipping")},
        company="Northwind Analytics",
        kind=JobUrlKind.ATS,
    )

    assert result.status is CareerPageStatus.REJECTED
    assert "does not appear to belong" in result.reason


# --- Redirects: the final destination is what is judged ---------------------


@pytest.mark.asyncio
async def test_a_redirect_to_a_valid_careers_page_is_accepted() -> None:
    start = "https://www.northwind-analytics.com/careers"
    final = "https://careers.northwind-analytics.com/"
    result = await verdict(
        start,
        {
            start: Page(redirect_to=final),
            final: careers_page("Northwind Analytics"),
        },
        company="Northwind Analytics",
    )

    assert result.status is CareerPageStatus.VERIFIED
    assert result.url == final, "the user must be sent where the chain ended"


@pytest.mark.asyncio
async def test_a_redirect_into_a_login_page_is_rejected() -> None:
    """``workforcenow.adp.com/`` does exactly this in production."""
    start = "https://workforcenow.adp.com/"
    final = "https://online.adp.com/olp/login"
    result = await verdict(
        start,
        {start: Page(redirect_to=final), final: login_page("ADP")},
        company="United Planning Organization",
        kind=JobUrlKind.ATS,
    )

    assert result.status is CareerPageStatus.REJECTED


@pytest.mark.asyncio
async def test_a_redirect_into_another_company_is_rejected() -> None:
    start = "https://careers.northwind-analytics.com/"
    final = "https://careers.contoso.com/"
    result = await verdict(
        start,
        {start: Page(redirect_to=final), final: careers_page("Contoso Shipping")},
        company="Northwind Analytics",
    )

    assert result.status is CareerPageStatus.REJECTED


@pytest.mark.asyncio
async def test_a_redirect_loop_is_rejected_rather_than_followed_forever() -> None:
    a = "https://careers.acme.com/a"
    b = "https://careers.acme.com/b"
    result = await verdict(
        a, {a: Page(redirect_to=b), b: Page(redirect_to=a)}, company="Acme Robotics"
    )

    assert result.status is CareerPageStatus.REJECTED
    assert result.url is None


# --- Candidate expansion: a company is not dropped on one bad link ----------


@pytest.mark.asyncio
async def test_a_company_survives_its_first_url_failing() -> None:
    """Requirement: try the company's other observed URLs before giving up."""
    dead = admit_career_url(
        "https://boards.greenhouse.io/northwindanalytics/jobs/999",
        company="Northwind Analytics",
    )
    good = admit_career_url(
        "https://careers.northwind-analytics.com/job/12",
        company="Northwind Analytics",
    )
    assert dead is not None and good is not None

    fetcher = FakeCareerFetcher(
        pages={
            "https://boards.greenhouse.io/northwindanalytics": not_found_page(),
            "https://boards.greenhouse.io/northwindanalytics/jobs/999": (
                not_found_page()
            ),
            "https://careers.northwind-analytics.com/": careers_page(
                "Northwind Analytics"
            ),
        }
    )

    outcome = await validate_company(
        [dead, good], company="Northwind Analytics", fetcher=fetcher, now=NOW
    )

    assert outcome.result.status is CareerPageStatus.VERIFIED
    assert outcome.result.url == "https://careers.northwind-analytics.com/"
    assert len(outcome.attempted) > 1, "the first URL's failure must not end it"


@pytest.mark.asyncio
async def test_the_reported_reason_is_the_most_informative_one() -> None:
    """Not the last candidate's, which is an accident of ordering."""
    only = admit_career_url(
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4427391",
        company="AMBA",
    )
    assert only is not None

    fetcher = FakeCareerFetcher(
        pages={
            "https://recruiting.paylocity.com/Recruiting/Jobs": company_id_page(),
        }
    )

    outcome = await validate_company(
        [only], company="AMBA", fetcher=fetcher, now=NOW
    )

    assert outcome.result.status is CareerPageStatus.REJECTED
    assert "company or organisation ID" in outcome.result.reason


# --- No fabricated URLs -----------------------------------------------------


def test_no_candidate_is_invented_from_a_company_name() -> None:
    """Requirement H: never manufacture ``<company>.com/careers``.

    Every candidate must be a truncation of the observed URL, so its host is
    always the observed host. A company name that happens to suggest a domain
    cannot produce one.
    """
    observed = "https://boards.greenhouse.io/northwindanalytics/jobs/4155555"
    candidates = career_url_candidates(
        observed, kind=JobUrlKind.ATS, company="Northwind Analytics"
    )

    assert candidates
    for candidate in candidates:
        assert candidate.startswith("https://boards.greenhouse.io/")
        assert observed.startswith(candidate.rstrip("/")) or candidate == observed
    assert not any("northwind-analytics.com" in url for url in candidates)


def test_a_company_with_only_aggregator_links_yields_no_candidates() -> None:
    """Jobright and friends are refused before any request is made."""
    assert (
        admit_career_url(
            "https://jobright.ai/jobs/info/6a873f2fcde3717f9e9b103c",
            company="Texas Department of State Health Services",
        )
        is None
    )


# --- Safety -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8100/careers",
        "http://127.0.0.1/careers",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/careers",
        "http://10.0.0.5/jobs",
        "file:///etc/passwd",
        "ftp://careers.acme.com/",
        "gopher://careers.acme.com/",
    ],
)
def test_local_and_internal_addresses_are_refused(url: str) -> None:
    """A portal supplies these strings, so they are treated as hostile input."""
    safe, reason = address_is_safe(url)

    assert safe is False
    assert reason


@pytest.mark.asyncio
async def test_a_redirect_into_a_private_address_is_refused() -> None:
    """The check runs again after every hop, which is why redirects are manual."""
    start = "https://careers.acme.com/"
    result = await verdict(
        start,
        {start: Page(redirect_to="http://127.0.0.1:8100/admin")},
        company="Acme Robotics",
    )

    assert result.status is not CareerPageStatus.VERIFIED


# --- robots.txt -------------------------------------------------------------


def test_consecutive_user_agent_lines_form_one_group() -> None:
    """The parser bug that silently turns "disallowed" into "allowed"."""
    policy = RobotsPolicy.parse(
        "User-agent: BadBot\nUser-agent: *\nDisallow: /\n",
        user_agent="JobFinderBot/0.1 (+contact)",
    )

    assert policy.allows("https://example.com/careers") is False


def test_the_longest_matching_rule_wins_not_the_first() -> None:
    policy = RobotsPolicy.parse(
        "User-agent: *\nDisallow: /jobs/\nAllow: /jobs/search\n",
        user_agent="JobFinderBot/0.1 (+contact)",
    )

    assert policy.allows("https://example.com/jobs/search") is True
    assert policy.allows("https://example.com/jobs/private") is False


def test_a_group_naming_us_beats_the_wildcard_group() -> None:
    policy = RobotsPolicy.parse(
        "User-agent: *\nDisallow: /\n\nUser-agent: JobFinderBot\nAllow: /\n",
        user_agent="JobFinderBot/0.1 (+contact)",
    )

    assert policy.allows("https://example.com/careers") is True


def test_an_empty_disallow_does_not_block_everything() -> None:
    """``Disallow:`` with no value means "nothing is disallowed"."""
    policy = RobotsPolicy.parse(
        "User-agent: *\nDisallow:\n", user_agent="JobFinderBot/0.1 (+contact)"
    )

    assert policy.allows("https://example.com/careers") is True


def test_a_wildcard_and_anchor_are_honoured() -> None:
    policy = RobotsPolicy.parse(
        "User-agent: *\nDisallow: /*.pdf$\nAllow: /\n",
        user_agent="JobFinderBot/0.1 (+contact)",
    )

    assert policy.allows("https://example.com/handbook.pdf") is False
    assert policy.allows("https://example.com/careers") is True


def test_a_rule_may_begin_with_a_wildcard() -> None:
    """The real file that exposed this, from UKG and UltiPro.

    A blanket ``Disallow: /`` with a wildcard-leading ``Allow`` carving out the
    job boards. Dropping rules that did not start with "/" left only the
    blanket refusal, so every employer on those two products - several hundred
    collected postings - looked forbidden while being explicitly permitted.
    """
    policy = RobotsPolicy.parse(
        """
        User-agent: *
        Disallow: /
        Allow: */JobBoard/
        Disallow: */JobBoardView
        Disallow: */JobBoard/*/Styles
        """,
        user_agent="JobFinderBot/0.1 (+contact)",
    )

    board = "https://recruiting2.ultipro.com/ACM1016ACME/JobBoard/3347ce03"
    assert policy.allows(board) is True
    assert policy.allows(board + "/Styles/app.css") is False
    assert policy.allows("https://recruiting2.ultipro.com/x/JobBoardView") is False
    assert policy.allows("https://recruiting2.ultipro.com/anything-else") is False


def test_an_unreadable_robots_file_means_we_do_not_fetch() -> None:
    assert RobotsPolicy.unreadable_policy().allows("https://example.com/x") is False


# --- The whole lane: companies in, verified companies out -------------------


def _discovered(name: str, url: str, *, relevance: float = 95.0) -> CareerPage:
    return CareerPage(
        company_name=name,
        company_key=name.casefold().replace(" ", ""),
        relevant_role="Financial Analyst",
        location="Chicago, IL",
        official_career_url="",
        url_kind=JobUrlKind.EMPLOYER_CAREER,
        opening_count=2,
        relevance=relevance,
        reason="2 current US openings matching Financial Analyst.",
        status=CareerPageStatus.DISCOVERED,
        candidate_urls=(url,),
    )


def _pool(count: int) -> CareerPageDiscovery:
    discovery = CareerPageDiscovery()
    discovery.pages = [
        _discovered(
            f"Company {index:02d} Holdings",
            f"https://careers.company{index:02d}holdings.com/job/1",
            # Descending, so the ranking is observable in the output order.
            relevance=99.0 - index,
        )
        for index in range(count)
    ]
    discovery.candidate_ceiling = 20
    return discovery


def _serving(count: int, *, good: int) -> FakeCareerFetcher:
    pages: dict[str, Page] = {}
    for index in range(count):
        root = f"https://careers.company{index:02d}holdings.com/"
        name = f"Company {index:02d} Holdings"
        pages[root] = careers_page(name) if index < good else login_page(name)
    return FakeCareerFetcher(pages=pages)


@pytest.mark.asyncio
async def test_thirty_valid_companies_return_the_best_twenty(db_session) -> None:
    discovery = _pool(30)
    pages = await verify_career_pages(
        db_session, discovery, fetcher=_serving(30, good=30), now=NOW
    )

    assert len(pages) == 20
    assert all(page.is_verified for page in pages)
    # Ranked as discovery ranked them: the strongest evidence first.
    assert [page.company_name for page in pages[:3]] == [
        "Company 00 Holdings",
        "Company 01 Holdings",
        "Company 02 Holdings",
    ]


@pytest.mark.asyncio
async def test_twenty_valid_companies_return_twenty_different_companies(
    db_session,
) -> None:
    discovery = _pool(20)
    pages = await verify_career_pages(
        db_session, discovery, fetcher=_serving(20, good=20), now=NOW
    )

    assert len(pages) == 20
    assert len({page.company_name for page in pages}) == 20
    assert len({page.official_career_url for page in pages}) == 20


@pytest.mark.asyncio
async def test_only_eight_valid_companies_returns_eight_and_pads_nothing(
    db_session,
) -> None:
    """Requirement: never fill the remaining slots to reach twenty."""
    discovery = _pool(25)
    pages = await verify_career_pages(
        db_session, discovery, fetcher=_serving(25, good=8), now=NOW
    )

    assert len(pages) == 8
    assert all(page.is_verified and page.official_career_url for page in pages)
    assert discovery.rejected_companies == 17


@pytest.mark.asyncio
async def test_a_company_that_fails_verification_carries_no_url_at_all(
    db_session,
) -> None:
    """Nothing downstream can render a link that is not there."""
    discovery = _pool(1)
    pages = await verify_career_pages(
        db_session, discovery, fetcher=_serving(1, good=0), now=NOW
    )

    assert pages == []
    assert discovery.pages[0].official_career_url == ""


@pytest.mark.asyncio
async def test_companies_past_the_ceiling_are_never_fetched(db_session) -> None:
    """The ceiling is cost control as well as a result limit."""
    fetcher = _serving(40, good=40)
    await verify_career_pages(db_session, _pool(40), fetcher=fetcher, now=NOW)

    assert len(fetcher.requested) < 40, (
        "once twenty companies verified, the rest must not be requested"
    )


# --- The cache ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_verdict_is_cached_and_not_refetched(db_session) -> None:
    first = _serving(3, good=3)
    await verify_career_pages(db_session, _pool(3), fetcher=first, now=NOW)
    assert first.requested

    second = _serving(3, good=3)
    pages = await verify_career_pages(
        db_session, _pool(3), fetcher=second, now=NOW + timedelta(minutes=5)
    )

    assert len(pages) == 3
    assert second.requested == [], "a fresh verdict must be reused, not re-fetched"


@pytest.mark.asyncio
async def test_a_cached_failure_expires_so_a_company_can_come_back(
    db_session,
) -> None:
    """Requirement: a cached failure must not permanently eliminate a company.

    The first run cannot reach the site, which is a statement about our request
    and not about the employer, so it is held only briefly. An hour later the
    company is checked again - and passes.
    """
    unreachable = FakeCareerFetcher(pages={})
    assert await verify_career_pages(
        db_session, _pool(1), fetcher=unreachable, now=NOW
    ) == []

    later = _serving(1, good=1)
    pages = await verify_career_pages(
        db_session, _pool(1), fetcher=later, now=NOW + timedelta(hours=1)
    )

    assert len(pages) == 1
    assert later.requested, "the company must be re-checked, not written off"


@pytest.mark.asyncio
async def test_a_verified_page_is_revalidated_once_it_goes_stale(db_session) -> None:
    """A page proven good today is not trusted indefinitely."""
    first = _serving(1, good=1)
    await verify_career_pages(db_session, _pool(1), fetcher=first, now=NOW)

    gone = _serving(1, good=0)
    pages = await verify_career_pages(
        db_session, _pool(1), fetcher=gone, now=NOW + timedelta(days=1)
    )

    assert gone.requested, "a stale verdict must be re-checked"
    assert pages == [], "and the page that has since closed must drop out"
