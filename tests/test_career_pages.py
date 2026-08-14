"""Career Pages: companies and their official career pages.

A different kind of answer from a job search, and these tests exist mainly to
hold that difference in place. Two properties matter more than any single
behaviour:

* **A career page is never a vacancy.** It carries no score, no posted date and
  no application record, and it never enters the allocation that fills
  Find 20/50/100.
* **A career page URL is the employer's.** Anything that cannot be shown to
  belong to the company is refused, and the result set is allowed to be short
  rather than padded with something unverified.

No company is fixed anywhere in the implementation, so every company in these
tests is invented for the test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.careers import discover_career_pages, verify_career_url
from app.careers.base import CareerPageRequest, career_registry
from app.careers.discovery import MAX_CAREER_PAGES, MIN_CAREER_RELEVANCE
from app.careers.source import LiveListingCareerSource
from app.core.config import Settings
from app.core.constants import ApplicationStatus, JobUrlKind, Seniority, SkillKind
from app.matching.profile import ResumeMatchProfile
from app.models.application import Application
from app.models.portal import Portal
from app.models.profile import ResumeSkill
from app.portals.base import RawJob
from app.services import search_run_service
from app.services.portal_seed import PORTAL_SEEDS, ensure_portals_seeded
from app.services.search_orchestrator import execute_search_run
from app.services.user_service import get_or_create_default_user
from tests.conftest import TEST_RECORD_MARKER
from tests.factories import make_job, make_portal, make_profile, make_resume
from tests.fake_portals import build_fake_portal, make_raw_job

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

CAREER_SLUG = LiveListingCareerSource.slug

#: Matches the orchestration suite, so a listing that qualifies there
#: qualifies here and the mixed-search assertions are not vacuous.
RESUME_SKILLS = ("excel", "sql", "forecasting", "budgeting", "financial modeling")


# --- Fixtures ---------------------------------------------------------------


def analyst_profile() -> ResumeMatchProfile:
    """A Financial Analyst candidate, with no database behind it."""
    return ResumeMatchProfile(
        resume_id=uuid.uuid4(),
        target_role_key="financial_analyst",
        target_role_display="Financial Analyst",
        domain_key="finance",
        seniority=Seniority.MID,
        years_experience=5.0,
    )


def request_for(profile: ResumeMatchProfile, *, limit: int = MAX_CAREER_PAGES):
    return CareerPageRequest(
        queries=("Financial Analyst",),
        role_display="Financial Analyst",
        role_key="financial_analyst",
        limit=limit,
        profile=profile,
    )


def listing(
    *,
    title: str = "Financial Analyst",
    company: str,
    location: str = "Austin, TX",
    application_url: str,
    source: str = "fakeboard",
) -> RawJob:
    """One collected posting, shaped as a portal returns it."""
    return RawJob(
        external_id=None,
        source=source,
        title=title,
        company=company,
        location=location,
        description=None,
        source_url="https://fakeboard.invalid/jobs/1",
        application_url=application_url,
        posted_at=NOW,
        employment_type=None,
    )


def discover(listings, *, profile=None, limit=MAX_CAREER_PAGES):
    resolved = profile or analyst_profile()
    return discover_career_pages(
        listings,
        profile=resolved,
        request=request_for(resolved, limit=limit),
        now=NOW,
        limit=limit,
    )


async def analysed_resume(session: AsyncSession, *, user=None):
    """A Financial Analyst resume with enough profile to be scoreable.

    Same shape as the orchestration suite's helper: the skills matter, because
    a profile too thin to clear the mandatory-coverage gate would qualify no
    jobs and every mixed-search assertion would pass vacuously.
    """
    from tests.factories import make_user

    owner = user or await make_user(session)
    resume = await make_resume(session, user=owner)
    profile = await make_profile(session, resume=resume)
    profile.years_experience = 5.0
    profile.seniority = Seniority.MID
    profile.education = ["Bachelor of Science in Finance"]

    for key in RESUME_SKILLS:
        session.add(
            ResumeSkill(
                profile_id=profile.id,
                skill_key=key,
                display=key.title(),
                kind=SkillKind.SKILL,
                occurrences=2,
                confidence=0.9,
            )
        )

    await session.flush()
    return owner, resume


# --- The result is a company, not a job -------------------------------------


def test_a_career_page_carries_the_four_things_it_must() -> None:
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                location="Austin, TX",
                application_url="https://careers.northwind-analytics.com/job/12",
            )
        ]
    )

    assert outcome.returned_count == 1
    page = outcome.pages[0]
    assert page.company_name == "Northwind Analytics"
    assert page.relevant_role == "Financial Analyst"
    assert page.location == "Austin, TX"
    assert page.official_career_url.startswith("https://careers.northwind-analytics.com")
    # And the things a vacancy has that a company page must not pretend to.
    assert not hasattr(page, "final_score")
    assert not hasattr(page, "posted_at")
    assert not hasattr(page, "application_status")


def test_the_reason_states_the_evidence_rather_than_a_match_percentage() -> None:
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                application_url="https://careers.northwind-analytics.com/job/1",
            ),
            listing(
                title="FP&A Analyst",
                company="Northwind Analytics",
                application_url="https://careers.northwind-analytics.com/job/2",
            ),
        ]
    )

    page = outcome.pages[0]
    assert page.opening_count == 2
    assert "2 current US openings" in page.reason
    # A company-level judgement, never dressed up as a resume match.
    assert "% match" not in page.reason
    assert "resume match" not in page.reason.lower()


# --- Official career pages only (requirement 3) -----------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/northwind/jobs",
        "https://www.indeed.com/cmp/Northwind",
        "https://www.glassdoor.com/Overview/northwind.htm",
        "https://www.ziprecruiter.com/c/Northwind",
        "https://jobright.ai/jobs/info/abc",
        "https://hiring.cafe/companies/northwind",
        "https://scale.jobs/company/northwind",
        "https://www.simplyhired.com/search?q=northwind",
        "https://www.builtin.com/company/northwind",
        "https://talent.com/jobs?k=northwind",
    ],
)
def test_third_party_job_boards_are_refused(url: str) -> None:
    assert verify_career_url(url, company="Northwind Analytics") is None


def test_a_rejected_url_removes_the_company_entirely() -> None:
    """No fallback to the aggregator link. The company is simply not shown."""
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                application_url="https://www.indeed.com/viewjob?jk=abc",
            )
        ]
    )

    assert outcome.returned_count == 0
    assert outcome.unverified_companies == 1


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url at all",
        "javascript:alert(1)",
        "mailto:jobs@northwind-analytics.com",
        "ftp://careers.northwind-analytics.com",
        "https://",
    ],
)
def test_a_malformed_url_is_never_silently_accepted(url: str) -> None:
    assert verify_career_url(url, company="Northwind Analytics") is None


def test_an_unrecognised_host_is_refused_rather_than_assumed_official() -> None:
    """The failure direction that matters.

    A host nobody can tie to the employer might be their careers site, or might
    be anything at all. Guessing would put an unverified link under a heading
    that promises the company's own page.
    """
    assert verify_career_url("https://random-listings.example/x", company="Acme Corp") is None


def test_a_bare_ats_root_is_not_a_company_career_page() -> None:
    """``boards.greenhouse.io`` is Greenhouse's front door, not an employer's."""
    assert verify_career_url("https://boards.greenhouse.io", company="Acme Corp") is None
    assert verify_career_url("https://boards.greenhouse.io/", company="Acme Corp") is None


def test_a_company_specific_ats_board_is_accepted_and_trimmed() -> None:
    verified = verify_career_url(
        "https://boards.greenhouse.io/northwindanalytics/jobs/4155555",
        company="Northwind Analytics",
    )

    assert verified is not None
    assert verified.kind is JobUrlKind.ATS
    # The employer's board root, not the one vacancy that happened to be seen -
    # a career page has to outlive the posting that revealed it.
    assert verified.url == "https://boards.greenhouse.io/northwindanalytics"
    assert verified.trimmed is True


def test_an_employer_careers_host_is_accepted_and_trimmed_to_its_root() -> None:
    verified = verify_career_url(
        "https://careers.northwind-analytics.com/job/12345?utm_source=board",
        company="Northwind Analytics",
    )

    assert verified is not None
    assert verified.kind is JobUrlKind.EMPLOYER_CAREER
    assert verified.url == "https://careers.northwind-analytics.com/"


def test_a_lookalike_domain_cannot_claim_a_shorter_company_name() -> None:
    """The corroboration rule is one-way on purpose.

    "veevasystems" starting with "veeva" is evidence for Veeva. "apple" does
    not start with "applebees", so a restaurant chain's careers site cannot
    claim Apple - which the reverse test would have allowed.
    """
    assert verify_career_url("https://careers.applebees.com/j/1", company="Apple") is None
    assert (
        verify_career_url(
            "https://careers.veeva.com/j/1", company="Veeva Systems"
        )
        is not None
    )


def test_a_very_short_domain_label_is_not_evidence_on_its_own() -> None:
    """A four-letter label matches too many companies by accident.

    "bank" is a prefix of "Bank of America", "Bank of the West" and a hundred
    others, so ``careers.bank.com`` proves nothing about which of them it is.
    Refusing costs a card; accepting sends the user somewhere wrong.
    """
    assert verify_career_url("https://careers.bank.com/j/1", company="Bank of America") is None
    assert verify_career_url("https://careers.acme.com/j/1", company="Acme Robotics") is None


def test_a_url_with_no_company_to_check_it_against_is_refused() -> None:
    assert verify_career_url("https://careers.acme.com/j/1", company=None) is None
    assert verify_career_url("https://careers.acme.com/j/1", company="  ") is None


# --- Duplicate companies (requirement 11) -----------------------------------


def test_the_same_company_written_three_ways_becomes_one_card() -> None:
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                location="Austin, TX",
                application_url="https://careers.northwind-analytics.com/job/1",
            ),
            listing(
                company="Northwind Analytics, Inc.",
                location="Denver, CO",
                application_url="https://careers.northwind-analytics.com/job/2",
            ),
            listing(
                title="FP&A Analyst",
                company="Northwind",
                location="Remote, United States",
                application_url="https://careers.northwind-analytics.com/job/3",
            ),
        ]
    )

    assert outcome.returned_count == 1
    page = outcome.pages[0]
    assert page.opening_count == 3
    assert "Austin, TX" in page.location
    assert "Denver, CO" in page.location


def test_two_genuinely_different_companies_stay_separate() -> None:
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                application_url="https://careers.northwind-analytics.com/j/1",
            ),
            listing(
                company="Southgate Capital",
                application_url="https://careers.southgate-capital.com/j/1",
            ),
        ]
    )

    assert outcome.returned_count == 2
    assert {page.company_name for page in outcome.pages} == {
        "Northwind Analytics",
        "Southgate Capital",
    }


def test_evidence_from_several_portals_lands_on_one_card() -> None:
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                application_url="https://careers.northwind-analytics.com/j/1",
                source="jobright",
            ),
            listing(
                company="Northwind Analytics",
                application_url="https://careers.northwind-analytics.com/j/2",
                source="hiringcafe",
            ),
        ]
    )

    assert outcome.returned_count == 1
    assert set(outcome.pages[0].sources) == {"jobright", "hiringcafe"}


# --- Role and location (requirements 5 and 6) -------------------------------


@pytest.mark.parametrize(
    "title",
    ["Registered Nurse", "Software Engineer", "Warehouse Associate"],
)
def test_a_company_hiring_for_another_role_is_not_returned(title: str) -> None:
    outcome = discover(
        [
            listing(
                title=title,
                company="Northwind Analytics",
                application_url="https://careers.northwind-analytics.com/j/1",
            )
        ]
    )

    assert outcome.returned_count == 0
    assert outcome.off_role_openings == 1


@pytest.mark.parametrize(
    "title",
    [
        "Financial Analyst",
        "Senior Financial Analyst",
        "FP&A Analyst",
        "Finance Analyst",
    ],
)
def test_same_role_variations_are_accepted(title: str) -> None:
    outcome = discover(
        [
            listing(
                title=title,
                company="Northwind Analytics",
                application_url="https://careers.northwind-analytics.com/j/1",
            )
        ]
    )

    assert outcome.returned_count == 1


@pytest.mark.parametrize(
    "adjacent",
    ["Accountant", "Auditor", "Business Analyst", "Data Analyst", "Finance Manager"],
)
def test_the_role_is_not_broadened_into_an_adjacent_career(adjacent: str) -> None:
    """The same guarantee the job pipeline gives, from the same gate.

    Career Pages calls ``evaluate_role``; it does not carry a second, looser
    notion of what counts as the candidate's role (CLAUDE.md section 3).
    """
    outcome = discover(
        [
            listing(
                title=adjacent,
                company="Northwind Analytics",
                application_url="https://careers.northwind-analytics.com/j/1",
            )
        ]
    )

    assert outcome.returned_count == 0


@pytest.mark.parametrize(
    "location",
    [
        "London, United Kingdom",
        "Bengaluru, India",
        "Toronto, Ontario, Canada",
        "Remote - Germany",
    ],
)
def test_a_non_us_opening_does_not_produce_a_career_page(location: str) -> None:
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                location=location,
                application_url="https://careers.northwind-analytics.com/j/1",
            )
        ]
    )

    assert outcome.returned_count == 0
    assert outcome.non_us_openings == 1


def test_an_international_company_is_shown_only_for_its_us_openings() -> None:
    """A company is not returned for being global. It is returned for hiring here."""
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                location="London, United Kingdom",
                application_url="https://careers.northwind-analytics.com/j/1",
            ),
            listing(
                company="Northwind Analytics",
                location="Chicago, IL",
                application_url="https://careers.northwind-analytics.com/j/2",
            ),
        ]
    )

    assert outcome.returned_count == 1
    # Only the US opening counted as evidence.
    assert outcome.pages[0].opening_count == 1
    assert outcome.pages[0].location == "Chicago, IL"


# --- The cap is a ceiling, never a target -----------------------------------


def test_the_cap_is_twenty_and_is_not_the_requested_job_limit() -> None:
    assert MAX_CAREER_PAGES == 20

    listings = [
        listing(
            company=f"Company {index:02d} Holdings",
            application_url=f"https://careers.company{index:02d}holdings.com/j/1",
        )
        for index in range(35)
    ]

    assert discover(listings).returned_count == MAX_CAREER_PAGES


def test_fewer_qualifying_companies_returns_fewer_and_pads_nothing() -> None:
    """min(20, verified relevant companies), with no relaxation to reach 20."""
    listings = [
        listing(
            company=f"Company {index:02d} Holdings",
            application_url=f"https://careers.company{index:02d}holdings.com/j/1",
        )
        for index in range(7)
    ]

    outcome = discover(listings)

    assert outcome.returned_count == 7
    assert all(page.relevance >= MIN_CAREER_RELEVANCE for page in outcome.pages)


def test_nothing_qualifying_returns_nothing() -> None:
    outcome = discover(
        [
            listing(
                company="Northwind Analytics",
                application_url="https://www.indeed.com/viewjob?jk=1",
            ),
            listing(
                title="Registered Nurse",
                company="Mercy Health",
                application_url="https://careers.mercyhealth.com/j/1",
            ),
        ]
    )

    assert outcome.returned_count == 0
    assert outcome.pages == []


def test_no_listings_at_all_is_an_empty_result_not_an_error() -> None:
    assert discover([]).returned_count == 0


# --- Portal selection and the run (requirements 1, 2, 8, 16) ----------------


async def prepare_run(
    session: AsyncSession,
    *,
    resume,
    specs: list[tuple[str, object]],
    career: bool = False,
    limit: int = 20,
):
    """Create a run over the given fake portals, optionally plus Career Pages."""
    portal_ids: list[uuid.UUID] = []
    adapters: dict[uuid.UUID, object] = {}

    for slug, adapter in specs:
        portal = await make_portal(session, slug=slug, adapter_key=adapter.slug, options={})
        portal_ids.append(portal.id)
        adapters[portal.id] = adapter()

    if career:
        # Reused across runs in one test: the slug is unique, so a second
        # search in the same test must select the row the first one created.
        career_portal = await session.scalar(
            select(Portal).where(Portal.adapter_key == CAREER_SLUG)
        )
        if career_portal is None:
            career_portal = await make_portal(
                session, slug=CAREER_SLUG, adapter_key=CAREER_SLUG, options={}
            )
        portal_ids.append(career_portal.id)

    prepared = await search_run_service.create_search_run(
        session,
        resume=resume,
        portal_ids=portal_ids,
        requested_limit=limit,
        require_adapter=False,
    )
    return prepared, adapters


async def run_search(session, prepared, adapters, *, limit_now=NOW):
    return await execute_search_run(
        session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=limit_now,
    )


def career_listings(portal: str = "boardone") -> list[RawJob]:
    """Listings whose employers publish verifiable career pages."""
    return [
        make_raw_job(
            portal=portal,
            index=index,
            company=company,
            location="Chicago, IL",
            application_url=f"https://careers.{slug}.com/job/{index}",
            now=NOW,
        )
        for index, (company, slug) in enumerate(
            [
                ("Northwind Analytics", "northwind-analytics"),
                ("Southgate Capital", "southgate-capital"),
                ("Ridgeline Energy", "ridgeline-energy"),
            ]
        )
    ]


def test_the_career_source_is_registered_under_its_own_registry() -> None:
    assert career_registry.is_registered(CAREER_SLUG)
    assert career_registry.get(CAREER_SLUG) is LiveListingCareerSource
    assert LiveListingCareerSource.display_name == "Career Pages"


def test_career_pages_is_seeded_as_a_selectable_source() -> None:
    seed = next(item for item in PORTAL_SEEDS if item.slug == CAREER_SLUG)
    assert seed.is_enabled is True


async def test_the_dashboard_offers_career_pages_as_a_checkbox(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Requirement 1, end to end: it must be selectable in Find Matches.

    The interface hardcodes no board - it renders whatever ``GET /portals``
    reports as selectable - so this is the check that the checkbox appears.
    """
    await ensure_portals_seeded(db_session)
    await db_session.commit()

    items = (await api_client.get("/api/v1/portals")).json()["data"]["items"]
    career = next(item for item in items if item["slug"] == CAREER_SLUG)

    assert career["display_name"] == "Career Pages"
    assert career["is_selectable"] is True
    # The field whose absence is silent. `is_selectable` is
    # `is_enabled and is_implemented`, and `is_implemented` is only True when
    # the seeder found something registered for the slug; when it does not,
    # the row still seeds - with a null adapter_key and no checkbox, and no
    # error anywhere to say why.
    assert career["is_enabled"] is True
    assert career["is_implemented"] is True


async def test_seeding_gives_career_pages_a_resolvable_adapter_key(
    db_session: AsyncSession,
) -> None:
    """The row must name something the orchestrator can resolve at run time."""
    await ensure_portals_seeded(db_session)

    portal = await db_session.scalar(
        select(Portal).where(Portal.slug == CAREER_SLUG)
    )

    assert portal is not None
    assert portal.adapter_key == CAREER_SLUG
    assert career_registry.is_registered(portal.adapter_key)


def test_the_seed_script_import_path_registers_the_career_source() -> None:
    """Regression: Career Pages seeded unselectable and nothing said so.

    ``app/scripts/seed_portals.py`` imports ``app.portals.adapters`` to fill
    the portal registry. It imports nothing from ``app.careers`` - the career
    source is registered only as a side effect of ``portal_seed`` importing
    ``career_registry``, which pulls in ``app/careers/__init__.py``.

    That is a real dependency and an invisible one: break it and the seeder
    still runs, still reports "career_pages created", and still writes a row -
    just one with ``is_implemented`` False that never reaches the dashboard.

    Checked in a subprocess because this session has already imported
    everything, which would make the assertion vacuous in process.
    """
    import subprocess
    import sys

    probe = (
        "import app.portals.adapters\n"
        "from app.services.portal_seed import ensure_portals_seeded\n"
        "from app.careers.base import career_registry\n"
        "print(career_registry.is_registered('career_pages'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True", result.stdout


async def test_career_pages_can_be_selected_on_its_own(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    prepared, adapters = await prepare_run(db_session, resume=resume, specs=[], career=True)

    outcome = await run_search(db_session, prepared, adapters)

    # It ran as its own leg and returned no vacancies, which is correct: a
    # career source produces companies, not jobs.
    assert outcome.returned_count == 0
    assert len(prepared.legs) == 1


async def test_a_mixed_search_returns_jobs_and_career_pages(
    db_session: AsyncSession,
) -> None:
    """Requirement 16. Both categories, neither replacing the other."""
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("boardone", jobs=career_listings())

    prepared, adapters = await prepare_run(
        db_session, resume=resume, specs=[("boardone", adapter)], career=True
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count > 0, "normal job results must still be returned"
    assert outcome.career_page_count > 0, "career pages must be returned too"
    # And they are different objects, not the same rows twice.
    assert {page.company_name for page in outcome.career_pages}
    assert all(not hasattr(page, "job_id") for page in outcome.career_pages)


async def test_career_pages_do_not_consume_the_requested_job_count(
    db_session: AsyncSession,
) -> None:
    """Requirement 8, stated as an equality rather than an inequality.

    The same portal, the same listings, the same limit - once without Career
    Pages and once with it. The job results must be identical, because the
    career lane never touches the pool they are selected from.
    """
    _, resume = await analysed_resume(db_session)
    listings = career_listings()

    alone_prepared, alone_adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("solo", build_fake_portal("solo", jobs=listings))],
    )
    alone = await run_search(db_session, alone_prepared, alone_adapters)

    _, second_resume = await analysed_resume(db_session)
    both_prepared, both_adapters = await prepare_run(
        db_session,
        resume=second_resume,
        specs=[("mixed", build_fake_portal("mixed", jobs=listings))],
        career=True,
    )
    both = await run_search(db_session, both_prepared, both_adapters)

    assert both.returned_count == alone.returned_count
    assert [item.title for item in both.results] == [item.title for item in alone.results]
    assert [item.final_score for item in both.results] == [
        item.final_score for item in alone.results
    ]
    assert both.career_page_count > 0


async def test_an_unselected_career_source_changes_nothing(
    db_session: AsyncSession,
) -> None:
    """Requirements 3 and 15: the existing pipeline when Career Pages is off."""
    _, resume = await analysed_resume(db_session)
    prepared, adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("plain", build_fake_portal("plain", jobs=career_listings()))],
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.career_pages == []
    assert outcome.returned_count > 0
    assert len(outcome.portals) == 1


async def test_a_broken_career_source_does_not_fail_the_job_results(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One source failing must not fail the run (CLAUDE.md section 18)."""

    async def explode(*args, **kwargs):
        raise RuntimeError("career source unavailable")

    monkeypatch.setattr(LiveListingCareerSource, "discover", explode)

    _, resume = await analysed_resume(db_session)
    prepared, adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("resilient", build_fake_portal("resilient", jobs=career_listings()))],
        career=True,
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count > 0
    assert outcome.career_pages == []


# --- No application records (requirement 10) --------------------------------


async def test_returning_a_career_page_creates_no_application(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    before = await db_session.scalar(select(func.count()).select_from(Application))

    prepared, adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("tracked", build_fake_portal("tracked", jobs=career_listings()))],
        career=True,
    )
    outcome = await run_search(db_session, prepared, adapters)

    after = await db_session.scalar(select(func.count()).select_from(Application))

    assert outcome.career_page_count > 0
    assert after == before, "a career page must never create an application record"


async def test_career_pages_write_no_jobs_of_their_own(
    db_session: AsyncSession,
) -> None:
    """Selected alone, with nothing to collect, it stores nothing."""
    from app.models.job import Job

    _, resume = await analysed_resume(db_session)
    before = await db_session.scalar(select(func.count()).select_from(Job))

    prepared, adapters = await prepare_run(db_session, resume=resume, specs=[], career=True)
    await run_search(db_session, prepared, adapters)

    assert await db_session.scalar(select(func.count()).select_from(Job)) == before


# --- The existing guarantees, unchanged -------------------------------------


async def test_applied_jobs_stay_excluded_when_career_pages_is_selected(
    db_session: AsyncSession,
) -> None:
    user, resume = await analysed_resume(db_session)
    listings = career_listings()

    first_prepared, first_adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("keepone", build_fake_portal("keepone", jobs=listings))],
        career=True,
    )
    first = await run_search(db_session, first_prepared, first_adapters)
    await db_session.commit()
    assert first.returned_count > 0

    for item in first.results:
        db_session.add(
            Application(
                user_id=user.id,
                resume_id=resume.id,
                job_id=item.job_id,
                status=ApplicationStatus.APPLIED,
                applied_at=NOW,
                notes=TEST_RECORD_MARKER,
            )
        )
    await db_session.flush()

    second_prepared, second_adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("keeptwo", build_fake_portal("keeptwo", jobs=listings))],
        career=True,
    )
    second = await run_search(db_session, second_prepared, second_adapters)

    assert second.returned_count == 0, "applied jobs must not come back"
    # The company is still a company that is hiring, so its career page stays.
    assert second.career_page_count > 0


async def test_skipped_jobs_stay_excluded_when_career_pages_is_selected(
    db_session: AsyncSession,
) -> None:
    user, resume = await analysed_resume(db_session)
    listings = career_listings()

    first_prepared, first_adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("skipone", build_fake_portal("skipone", jobs=listings))],
        career=True,
    )
    first = await run_search(db_session, first_prepared, first_adapters)
    await db_session.commit()

    for item in first.results:
        db_session.add(
            Application(
                user_id=user.id,
                resume_id=resume.id,
                job_id=item.job_id,
                status=ApplicationStatus.SKIPPED,
                notes=TEST_RECORD_MARKER,
            )
        )
    await db_session.flush()

    second_prepared, second_adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("skiptwo", build_fake_portal("skiptwo", jobs=listings))],
        career=True,
    )
    second = await run_search(db_session, second_prepared, second_adapters)

    assert second.returned_count == 0, "skipped jobs must not come back"


async def test_the_seventy_percent_threshold_is_untouched(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    prepared, adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("threshold", build_fake_portal("threshold", jobs=career_listings()))],
        career=True,
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.min_score >= 70
    assert all(item.final_score >= 70.0 for item in outcome.results)


async def test_the_thirty_day_window_is_untouched(
    db_session: AsyncSession,
) -> None:
    """An over-age listing is still excluded, and does not become a career page
    by the back door either - the company has no *current* opening."""
    _, resume = await analysed_resume(db_session)
    stale = make_raw_job(
        portal="stale",
        index=0,
        company="Northwind Analytics",
        location="Chicago, IL",
        application_url="https://careers.northwind-analytics.com/job/1",
        now=NOW - timedelta(days=45),
    )

    prepared, adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("stale", build_fake_portal("stale", jobs=[stale]))],
        career=True,
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 0, "a 45-day-old listing is never eligible"


async def test_results_stay_ordered_newest_first_with_career_pages_selected(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    jobs = [
        make_raw_job(
            portal="ordered",
            index=index,
            company=f"Company {index} Holdings",
            location="Chicago, IL",
            age_days=age,
            now=NOW,
        )
        for index, age in enumerate([12.0, 1.0, 6.0])
    ]

    prepared, adapters = await prepare_run(
        db_session,
        resume=resume,
        specs=[("ordered", build_fake_portal("ordered", jobs=jobs))],
        career=True,
    )
    outcome = await run_search(db_session, prepared, adapters)

    dates = [item.posted_at for item in outcome.results]
    assert dates == sorted(dates, reverse=True)


# --- The API surface --------------------------------------------------------


async def test_the_search_response_carries_career_pages_separately(
    api_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """They are their own field, not extra rows in ``results``."""
    user = await get_or_create_default_user(db_session, settings)
    _, resume = await analysed_resume(db_session, user=user)
    await ensure_portals_seeded(db_session)
    career_portal = await db_session.scalar(
        select(Portal).where(Portal.slug == CAREER_SLUG)
    )
    await db_session.commit()

    response = await api_client.post(
        "/api/v1/search-runs",
        json={
            "resume_id": str(resume.id),
            "selected_portal_ids": [str(career_portal.id)],
            "requested_limit": 20,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()["data"]
    assert "career_pages" in body
    assert isinstance(body["career_pages"], list)
    # A career page never carries the fields a vacancy does.
    for page in body["career_pages"]:
        assert "job_id" not in page
        assert "final_score" not in page
        assert "application_status" not in page


def test_career_pages_need_no_new_table() -> None:
    """Requirement 13, asserted rather than left as a claim in a report.

    Career pages are transient discovery output. Nothing persists them, so no
    model was added and no migration exists for them.
    """
    from app.db.base import Base

    tables = set(Base.metadata.tables)
    assert not any("career" in name for name in tables)
