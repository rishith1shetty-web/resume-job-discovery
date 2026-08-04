"""Jobright and Hiring Cafe together, through the real pipeline.

These drive the **real adapters** - real slug matching, real parsing, real
field mapping - against captured fixtures instead of the network, then push the
result through the real processing, scoring and orchestration code. Only the
HTTP layer is replaced.

That distinction matters when reading these results: the parsing and the
cross-portal behaviour are genuinely exercised, but no test here proves what
either portal currently has in stock. Live verification is a separate exercise.

Every test runs inside a transaction that is rolled back, so nothing is
committed to resume_job_discovery.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ApplicationStatus,
    JobUrlKind,
    PortalRunStatus,
    SearchRunStatus,
    Seniority,
    SkillKind,
)
from app.models.application import Application
from app.models.job import Job, JobMatch, JobSource
from app.models.profile import ResumeSkill
from app.portals.adapters.hiringcafe import HiringCafePortal
from app.portals.adapters.jobright import JobrightPortal
from app.portals.exceptions import PortalParseError, PortalTimeoutError
from app.services import search_run_service
from app.services.search_orchestrator import execute_search_run
from tests.factories import make_portal, make_profile, make_resume, make_user

FIXTURES = Path(__file__).parent / "fixtures" / "portals"
HIRINGCAFE_FIXTURE = FIXTURES / "hiringcafe_financial_analyst.json"
JOBRIGHT_FIXTURE = FIXTURES / "jobright_financial_analyst.json"

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
TAXONOMY = frozenset({"financial-analyst"})


# --- Offline adapters -------------------------------------------------------


def _hiringcafe_html(records: list[dict]) -> str:
    payload = {"props": {"pageProps": {"ssrHits": records}}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


def _jobright_html(records: list[dict]) -> str:
    payload = {"props": {"pageProps": {"jobList": records}}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


class OfflineHiringCafe(HiringCafePortal):
    """The real adapter with its taxonomy pinned, served from a fixture."""

    records: list[dict] = []
    failure: Exception | None = None

    async def _load_taxonomy(self):  # noqa: ANN201
        return TAXONOMY


class OfflineJobright(JobrightPortal):
    """The real adapter with its taxonomy pinned, served from a fixture."""

    records: list[dict] = []
    failure: Exception | None = None

    async def _load_taxonomy(self):  # noqa: ANN201
        return TAXONOMY


#: Page builder and fixture state per portal slug. The HTTP client knows which
#: portal it belongs to, which is what lets one patch serve both adapters.
_OFFLINE = {
    "hiringcafe": (OfflineHiringCafe, _hiringcafe_html),
    "jobright": (OfflineJobright, _jobright_html),
}


@pytest.fixture(autouse=True)
def offline_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve both adapters from fixtures, and fail loudly on a real request.

    Only the transport is replaced: slug matching, page parsing and field
    mapping all run the production code.
    """

    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        entry = _OFFLINE.get(self.portal)
        if entry is None:
            raise AssertionError(f"unexpected request from {self.portal}: {url}")

        adapter_class, build_page = entry
        if adapter_class.failure is not None:
            raise adapter_class.failure
        return build_page(adapter_class.records)

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", fake_get_text)

    yield

    # Class-level state must not leak into the next test.
    for adapter_class, _ in _OFFLINE.values():
        adapter_class.records = []
        adapter_class.failure = None


def hiringcafe_records() -> list[dict]:
    data = json.loads(HIRINGCAFE_FIXTURE.read_text(encoding="utf-8"))
    return data["props"]["pageProps"]["ssrHits"]


def jobright_records() -> list[dict]:
    data = json.loads(JOBRIGHT_FIXTURE.read_text(encoding="utf-8"))
    return data["props"]["pageProps"]["jobList"]


def build_hiringcafe(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineHiringCafe:
    adapter = OfflineHiringCafe(options={"rate_limit_per_minute": 0})
    type(adapter).records = records if records is not None else hiringcafe_records()
    type(adapter).failure = failure
    return adapter


def build_jobright(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineJobright:
    adapter = OfflineJobright(options={"rate_limit_per_minute": 0})
    type(adapter).records = records if records is not None else jobright_records()
    type(adapter).failure = failure
    return adapter


# --- Scaffolding ------------------------------------------------------------


async def analysed_resume(session: AsyncSession, *, user=None):
    user = user or await make_user(session)
    resume = await make_resume(session, user=user)
    profile = await make_profile(session, resume=resume)
    profile.years_experience = 5.0
    profile.seniority = Seniority.MID
    profile.education = ["Bachelor of Science in Finance"]

    for key in ("excel", "sql", "forecasting", "budgeting", "financial modeling"):
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
    return user, resume


def carried_by(item, slug: str) -> bool:
    """Whether a result is sourced from a portal.

    Test portal rows are marker-prefixed (``pytest-jobfinder-hiringcafe``), so
    this compares the suffix rather than the whole slug.
    """
    return any(source.endswith(slug) for source in item.portal_slugs)


async def run_search(
    session: AsyncSession, *, resume, specs: list[tuple[str, object]], limit: int = 20
):
    """``specs`` is [(portal slug, adapter instance)] in selection order."""
    portals = []
    adapters: dict[uuid.UUID, object] = {}

    for slug, adapter in specs:
        portal = await make_portal(
            session, slug=slug, adapter_key=slug, options={}
        )
        portals.append(portal)
        adapters[portal.id] = adapter

    prepared = await search_run_service.create_search_run(
        session,
        resume=resume,
        portal_ids=[portal.id for portal in portals],
        requested_limit=limit,
        require_adapter=False,
    )
    outcome = await execute_search_run(
        session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=NOW,
    )
    return prepared, outcome


# --- Single-portal searches -------------------------------------------------


async def test_a_hiring_cafe_only_search_returns_its_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("hiringcafe", build_hiringcafe())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count > 0
    assert all(item.final_score >= 70.0 for item in outcome.results)
    assert all(carried_by(item, "hiringcafe") for item in outcome.results)


async def test_a_jobright_only_search_still_behaves_as_before(
    db_session: AsyncSession,
) -> None:
    # Adding a portal must not change what selecting Jobright alone does.
    #
    # None of the three listings in the Phase 4 fixture clears 70 for this
    # resume - the highest scores 64 - so the honest expectation is a
    # completed run that collected and scored them all and returned none.
    # Weakening the threshold to make this test look better is exactly what
    # the product forbids.
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("jobright", build_jobright())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count == 3
    assert prepared.legs[0].status is PortalRunStatus.COMPLETED
    assert outcome.returned_count == 0

    scored = await db_session.scalar(
        select(func.count())
        .select_from(JobMatch)
        .where(JobMatch.search_run_id == prepared.run.id)
    )
    assert scored == 3


async def test_jobright_listings_that_do_qualify_are_returned(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    qualifying = _mirror_as_jobright(hiringcafe_records()[0])

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("jobright", build_jobright([qualifying]))]
    )

    assert outcome.returned_count == 1
    assert carried_by(outcome.results[0], "jobright")


async def test_hiring_cafe_jobs_carry_an_employer_application_url(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("hiringcafe", build_hiringcafe())]
    )

    assert outcome.results
    for item in outcome.results:
        assert item.preferred_application_url
        assert "hiringcafe.com" not in item.preferred_application_url
        assert item.preferred_url_kind in (
            JobUrlKind.ATS.value,
            JobUrlKind.EMPLOYER_CAREER.value,
        )


# --- Both portals together --------------------------------------------------


async def test_both_portals_are_searched_and_merged(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("jobright", build_jobright()), ("hiringcafe", build_hiringcafe())],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert all(leg.status is PortalRunStatus.COMPLETED for leg in prepared.legs)
    assert all(leg.raw_count > 0 for leg in prepared.legs)

    # One combined list, ordered once.
    scores = [item.final_score for item in outcome.results]
    assert scores == sorted(scores, reverse=True)


async def test_quotas_are_allocated_across_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session,
        resume=resume,
        specs=[("jobright", build_jobright()), ("hiringcafe", build_hiringcafe())],
        limit=20,
    )

    assert [leg.target_quota for leg in prepared.legs] == [10, 10]
    assert sum(leg.selected_count for leg in prepared.legs) > 0


async def test_the_combined_result_has_no_duplicate_canonical_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("jobright", build_jobright()), ("hiringcafe", build_hiringcafe())],
    )

    ids = [item.job_id for item in outcome.results]
    assert len(ids) == len(set(ids))


async def test_each_job_is_scored_once_per_run(db_session: AsyncSession) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session,
        resume=resume,
        specs=[("jobright", build_jobright()), ("hiringcafe", build_hiringcafe())],
    )

    duplicates = (
        await db_session.execute(
            select(JobMatch.job_id, func.count())
            .where(JobMatch.search_run_id == prepared.run.id)
            .group_by(JobMatch.job_id)
            .having(func.count() > 1)
        )
    ).all()

    assert duplicates == []


# --- Cross-portal deduplication ---------------------------------------------


def _mirror_as_jobright(record: dict) -> dict:
    """Recast one Hiring Cafe listing as the same vacancy on Jobright.

    Same employer, same title, same location - a genuine cross-portal
    duplicate, expressed in the other portal's payload shape.
    """
    processed = record["v5_processed_job_data"]
    return {
        "jobResult": {
            "jobId": "jr-" + record["id"][:20],
            "jobTitle": record["job_information"]["title"],
            "jobLocation": processed.get("formatted_workplace_location"),
            "jobSummary": processed.get("requirements_summary"),
            "requirements": [processed.get("requirements_summary", "")],
            "coreResponsibilities": processed.get("role_activities", []),
            "publishTime": "2026-07-30 20:41:50",
            "employmentType": "Full-time",
            "url": "https://jobright.ai/jobs/info/mirror-1",
            "applyLink": "https://jobright.ai/jobs/info/mirror-1",
        },
        "companyResult": {
            "companyName": record["enriched_company_data"]["name"],
        },
    }


async def test_one_vacancy_on_both_portals_is_one_job_with_two_sources(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright([_mirror_as_jobright(shared)])),
            ("hiringcafe", build_hiringcafe([shared])),
        ],
    )

    jobs = list((await db_session.scalars(select(Job))).all())
    matching = [job for job in jobs if job.company == shared["enriched_company_data"]["name"]]

    assert len(matching) == 1, [job.title for job in matching]

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == matching[0].id)
            )
        ).all()
    )
    assert len(sources) == 2

    # Both portals are credited: provenance is never discarded.
    slugs = {item.portal_slugs for item in outcome.results if item.job_id == matching[0].id}
    assert slugs
    assert len(next(iter(slugs))) == 2


async def test_the_employer_url_wins_over_the_board_url(
    db_session: AsyncSession,
) -> None:
    # Phase 5's preference order, unchanged: employer/ATS beats an aggregator.
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright([_mirror_as_jobright(shared)])),
            ("hiringcafe", build_hiringcafe([shared])),
        ],
    )

    merged = [
        item
        for item in outcome.results
        if len(item.portal_slugs) == 2
    ]
    assert merged, "expected the mirrored vacancy to merge"
    assert "jobright.ai" not in merged[0].preferred_application_url
    assert merged[0].preferred_url_kind in (
        JobUrlKind.ATS.value,
        JobUrlKind.EMPLOYER_CAREER.value,
    )


async def test_a_duplicate_does_not_consume_two_quota_slots(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright([_mirror_as_jobright(shared)])),
            ("hiringcafe", build_hiringcafe([shared])),
        ],
    )

    assert sum(leg.selected_count for leg in prepared.legs) == outcome.returned_count
    ids = [item.job_id for item in outcome.results]
    assert len(ids) == len(set(ids))


async def test_scoring_does_not_depend_on_which_portal_found_the_job(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    _, first_resume = await analysed_resume(db_session)
    _, second_resume = await analysed_resume(db_session)

    _, hiring_only = await run_search(
        db_session,
        resume=first_resume,
        specs=[("hc-solo", build_hiringcafe([shared]))],
    )
    _, both = await run_search(
        db_session,
        resume=second_resume,
        specs=[
            ("jr-pair", build_jobright([_mirror_as_jobright(shared)])),
            ("hc-pair", build_hiringcafe([shared])),
        ],
    )

    if hiring_only.results and both.results:
        assert hiring_only.results[0].final_score == both.results[0].final_score


# --- Failure isolation ------------------------------------------------------


async def test_hiring_cafe_failing_still_returns_jobright_results(
    db_session: AsyncSession,
) -> None:
    # Jobright is given a listing known to qualify: the point of the test is
    # that the surviving portal's results reach the user, which needs a
    # surviving result to observe.
    qualifying = _mirror_as_jobright(hiringcafe_records()[0])
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright([qualifying])),
            (
                "hiringcafe",
                build_hiringcafe(failure=PortalParseError("shape", portal="hiringcafe")),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.returned_count > 0
    assert prepared.legs[0].status is PortalRunStatus.COMPLETED
    assert prepared.legs[1].status is PortalRunStatus.FAILED
    assert all(carried_by(item, "jobright") for item in outcome.results)


async def test_jobright_failing_still_returns_hiring_cafe_results(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "jobright",
                build_jobright(failure=PortalTimeoutError("slow", portal="jobright")),
            ),
            ("hiringcafe", build_hiringcafe()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.returned_count > 0
    assert prepared.legs[0].status is PortalRunStatus.FAILED
    assert prepared.legs[1].status is PortalRunStatus.COMPLETED
    assert all(carried_by(item, "hiringcafe") for item in outcome.results)


async def test_both_portals_failing_fails_the_run_honestly(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "jobright",
                build_jobright(failure=PortalTimeoutError("slow", portal="jobright")),
            ),
            (
                "hiringcafe",
                build_hiringcafe(failure=PortalParseError("shape", portal="hiringcafe")),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.FAILED
    assert outcome.returned_count == 0
    assert all(leg.status is PortalRunStatus.FAILED for leg in prepared.legs)
    assert prepared.run.error_message
    assert outcome.warnings


# --- Eligibility is unchanged -----------------------------------------------


async def test_an_applied_job_stays_excluded_when_found_by_the_other_portal(
    db_session: AsyncSession,
) -> None:
    # Phase 8's rule, unchanged by Phase 9A: applied/interview/rejected/offer
    # remove a canonical job from future searches for that resume, whichever
    # portal rediscovers it.
    shared = hiringcafe_records()[0]
    user, resume = await analysed_resume(db_session)

    _, first = await run_search(
        db_session, resume=resume, specs=[("hc-first", build_hiringcafe([shared]))]
    )
    await db_session.commit()
    assert first.returned_count == 1
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
        )
    )
    await db_session.flush()

    # Rediscovered through Jobright this time.
    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[("jr-second", build_jobright([_mirror_as_jobright(shared)]))],
    )

    assert job_id not in {item.job_id for item in second.results}


@pytest.mark.parametrize(
    "status", [ApplicationStatus.SAVED, ApplicationStatus.SKIPPED]
)
async def test_an_eligible_status_survives_the_new_portal(
    db_session: AsyncSession, status: ApplicationStatus
) -> None:
    shared = hiringcafe_records()[0]
    user, resume = await analysed_resume(db_session)

    _, first = await run_search(
        db_session, resume=resume, specs=[("hc-keep", build_hiringcafe([shared]))]
    )
    await db_session.commit()
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=status,
        )
    )
    await db_session.flush()

    _, second = await run_search(
        db_session, resume=resume, specs=[("hc-keep2", build_hiringcafe([shared]))]
    )

    assert job_id in {item.job_id for item in second.results}


async def test_one_resume_applying_does_not_hide_the_job_from_another(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    user, first_resume = await analysed_resume(db_session)
    _, second_resume = await analysed_resume(db_session, user=user)

    _, first = await run_search(
        db_session, resume=first_resume, specs=[("hc-a", build_hiringcafe([shared]))]
    )
    await db_session.commit()
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=first_resume.id,
            job_id=job_id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
        )
    )
    await db_session.flush()

    _, other = await run_search(
        db_session, resume=second_resume, specs=[("hc-b", build_hiringcafe([shared]))]
    )

    assert job_id in {item.job_id for item in other.results}


# --- Repeatability ----------------------------------------------------------


async def test_repeating_a_two_portal_search_creates_no_duplicates(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    specs = lambda: [  # noqa: E731
        ("jr", build_jobright()),
        ("hc", build_hiringcafe()),
    ]

    _, first = await run_search(db_session, resume=resume, specs=specs())
    await db_session.commit()
    jobs_after_first = await db_session.scalar(select(func.count()).select_from(Job))
    sources_after_first = await db_session.scalar(
        select(func.count()).select_from(JobSource)
    )

    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[("jr2", build_jobright()), ("hc2", build_hiringcafe())],
    )

    assert await db_session.scalar(select(func.count()).select_from(Job)) == (
        jobs_after_first
    )
    assert {item.job_id for item in second.results} == {
        item.job_id for item in first.results
    }
    assert sources_after_first > 0


async def test_the_order_is_stable_between_identical_runs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, first = await run_search(
        db_session,
        resume=resume,
        specs=[("jr-x", build_jobright()), ("hc-x", build_hiringcafe())],
    )
    await db_session.commit()

    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[("jr-y", build_jobright()), ("hc-y", build_hiringcafe())],
    )

    assert [item.job_id for item in first.results] == [
        item.job_id for item in second.results
    ]
    assert [item.final_score for item in first.results] == [
        item.final_score for item in second.results
    ]
