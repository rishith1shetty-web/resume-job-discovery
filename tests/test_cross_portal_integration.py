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
from app.models.job import Job, JobMatch, JobSource, RawJobRecord
from app.models.profile import ResumeSkill
from app.portals.adapters.hiringcafe import HiringCafePortal
from app.portals.adapters.greenhouse import GreenhousePortal
from app.portals.adapters.jobright import JobrightPortal
from app.portals.adapters.scalejobs import PAGE_SIZE as SCALEJOBS_PAGE_SIZE
from app.portals.adapters.scalejobs import ScaleJobsPortal
from app.portals.adapters.simplyhired import SimplyHiredPortal
from app.portals.adapters import ziprecruiter as ziprecruiter_module
from app.portals.adapters.ziprecruiter import ZipRecruiterPortal
from app.portals.exceptions import PortalParseError, PortalTimeoutError
from app.processing.normalize import RejectionReason
from app.services import search_run_service
from app.services.search_orchestrator import execute_search_run
from tests.factories import make_portal, make_profile, make_resume, make_user

FIXTURES = Path(__file__).parent / "fixtures" / "portals"
HIRINGCAFE_FIXTURE = FIXTURES / "hiringcafe_financial_analyst.json"
JOBRIGHT_FIXTURE = FIXTURES / "jobright_financial_analyst.json"
ZIPRECRUITER_FIXTURE = FIXTURES / "ziprecruiter_financial_analyst.json"
SIMPLYHIRED_FIXTURE = FIXTURES / "simplyhired_financial_analyst.json"
GREENHOUSE_FIXTURE = FIXTURES / "greenhouse_financial_analyst.json"
SCALEJOBS_FIXTURE = FIXTURES / "scalejobs_financial_analyst.json"

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is NOW, for relative-date portals.

    Only ``now`` is replaced; construction, arithmetic and comparison are the
    real ones, so an adapter patched with this behaves normally in every other
    respect.
    """

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - matches datetime.now
        return NOW if tz is None else NOW.astimezone(tz)
TAXONOMY = frozenset({"financial-analyst"})


# --- Offline adapters -------------------------------------------------------


def _hiringcafe_html(records: list[dict]) -> str:
    payload = {"props": {"pageProps": {"ssrHits": records}}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


def _simplyhired_html(records: list[dict]) -> str:
    # One page only: no cursor for a page two, so the adapter stops.
    payload = {
        "props": {
            "pageProps": {
                "jobs": records,
                "pageCursors": {"1": "page-one"},
                "currentPageNumber": 1,
            }
        }
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


def _greenhouse_body(records: list[dict]) -> str:
    # The board API answers with JSON, not a page.
    return json.dumps({"jobs": records, "meta": {"total": len(records)}})


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


class OfflineZipRecruiter(ZipRecruiterPortal):
    """The real adapter, served from a canned MCP envelope."""

    records: list[dict] = []
    failure: Exception | None = None


class OfflineSimplyHired(SimplyHiredPortal):
    """The real adapter, served from a canned search page."""

    records: list[dict] = []
    failure: Exception | None = None


class OfflineGreenhouse(GreenhousePortal):
    """The real adapter, served from a canned board payload."""

    records: list[dict] = []
    failure: Exception | None = None


class OfflineScaleJobs(ScaleJobsPortal):
    """The real adapter, served from a canned search response."""

    records: list[dict] = []
    failure: Exception | None = None


#: Page builder and fixture state per portal slug. The HTTP client knows which
#: portal it belongs to, which is what lets one patch serve every adapter.
_OFFLINE = {
    "hiringcafe": (OfflineHiringCafe, _hiringcafe_html),
    "jobright": (OfflineJobright, _jobright_html),
    "simplyhired": (OfflineSimplyHired, _simplyhired_html),
    "greenhouse": (OfflineGreenhouse, _greenhouse_body),
}

#: ZipRecruiter speaks JSON-RPC rather than serving pages, so it is stubbed at
#: the other verb. Listed separately because the reset loop covers both.
_OFFLINE_RPC = {"ziprecruiter": OfflineZipRecruiter}

#: Scale.jobs GETs JSON from a search endpoint, a third verb again.
_OFFLINE_JSON = {"scalejobs": OfflineScaleJobs}


@pytest.fixture(autouse=True)
def offline_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve every adapter from fixtures, and fail loudly on a real request.

    Only the transport is replaced: slug matching, page and envelope parsing
    and field mapping all run the production code.
    """

    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        entry = _OFFLINE.get(self.portal)
        if entry is None:
            raise AssertionError(f"unexpected request from {self.portal}: {url}")

        adapter_class, build_page = entry
        if adapter_class.failure is not None:
            raise adapter_class.failure
        return build_page(adapter_class.records)

    async def fake_post_json(self, url, *, payload, headers=None):  # noqa: ANN001
        adapter_class = _OFFLINE_RPC.get(self.portal)
        if adapter_class is None:
            raise AssertionError(f"unexpected request from {self.portal}: {url}")

        if adapter_class.failure is not None:
            raise adapter_class.failure
        if payload["method"] == "initialize":
            return {"result": {"protocolVersion": "2024-11-05", "capabilities": {}}}

        # The live endpoint pages in fives and then repeats its last page; the
        # fixture is small enough that one page is the whole of it.
        offset = payload["params"]["arguments"].get("offset", 0)
        records = adapter_class.records if not offset else []
        return {
            "result": {
                "structuredContent": {
                    "results": records,
                    "status": "OK",
                    "meta": {"count": len(records), "limit": 5, "total": len(records)},
                    "warnings": [],
                }
            }
        }

    async def fake_get_json(self, url, *, params=None):  # noqa: ANN001
        adapter_class = _OFFLINE_JSON.get(self.portal)
        if adapter_class is None:
            raise AssertionError(f"unexpected request from {self.portal}: {url}")

        if adapter_class.failure is not None:
            raise adapter_class.failure

        params = dict(params or {})
        records = adapter_class.records

        # Mirror the live endpoint's own filtering, so the freshness ladder and
        # the pagination are genuinely exercised rather than short-circuited.
        cutoff = params.get("dateFrom")
        if cutoff:
            records = [
                record
                for record in records
                if str(record.get("datePosted") or "")[:10] >= cutoff
            ]

        skip = int(params.get("skip", 0))
        take = int(params.get("take", SCALEJOBS_PAGE_SIZE))
        page = records[skip : skip + take]
        return {
            "jobs": page,
            "hasMore": len(records) > skip + take,
            "totalCount": len(records),
        }

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", fake_get_text)
    monkeypatch.setattr("app.portals.http.PortalHttpClient.post_json", fake_post_json)
    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_json", fake_get_json)

    yield

    # Class-level state must not leak into the next test.
    for adapter_class, _ in _OFFLINE.values():
        adapter_class.records = []
        adapter_class.failure = None
    for adapter_class in (*_OFFLINE_RPC.values(), *_OFFLINE_JSON.values()):
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


def ziprecruiter_records() -> list[dict]:
    data = json.loads(ZIPRECRUITER_FIXTURE.read_text(encoding="utf-8"))
    return data["Financial Analyst"][0]["results"]


def build_ziprecruiter(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineZipRecruiter:
    adapter = OfflineZipRecruiter(options={"rate_limit_per_minute": 0})
    type(adapter).records = records if records is not None else ziprecruiter_records()
    type(adapter).failure = failure
    return adapter


def simplyhired_records() -> list[dict]:
    data = json.loads(SIMPLYHIRED_FIXTURE.read_text(encoding="utf-8"))
    return data["searches"]["financial analyst"][0]["jobs"]


def simplyhired_detail() -> dict:
    return json.loads(SIMPLYHIRED_FIXTURE.read_text(encoding="utf-8"))["detail"]


def build_simplyhired(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineSimplyHired:
    adapter = OfflineSimplyHired(options={"rate_limit_per_minute": 0})
    type(adapter).records = records if records is not None else simplyhired_records()
    type(adapter).failure = failure
    return adapter


def greenhouse_records() -> list[dict]:
    data = json.loads(GREENHOUSE_FIXTURE.read_text(encoding="utf-8"))
    return [job for board in data.values() for job in board["jobs"]]


def build_greenhouse(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineGreenhouse:
    # One board keeps the fixture honest about request counts.
    adapter = OfflineGreenhouse(
        options={"rate_limit_per_minute": 0, "board_tokens": ["fixture-board"]}
    )
    type(adapter).records = records if records is not None else greenhouse_records()
    type(adapter).failure = failure
    return adapter


def scalejobs_records() -> list[dict]:
    data = json.loads(SCALEJOBS_FIXTURE.read_text(encoding="utf-8"))
    return data["Financial Analyst"][0]["jobs"]


def build_scalejobs(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineScaleJobs:
    adapter = OfflineScaleJobs(options={"rate_limit_per_minute": 0})
    type(adapter).records = records if records is not None else scalejobs_records()
    type(adapter).failure = failure
    return adapter


# --- Scaffolding ------------------------------------------------------------



def assert_ranked_newest_first(results) -> None:
    """The ranking contract: posted_at DESC, then final_score DESC.

    Replaces an older assertion that results were sorted by score alone. That
    is no longer the contract: recency is primary and match quality only breaks
    ties, so a newer weaker job legitimately precedes an older stronger one.
    Both keys are checked together, because asserting recency alone would pass
    for an ordering that ignored score entirely.
    """
    assert results, "nothing to check the ordering of"

    keys = [
        (-(item.posted_at.timestamp() if item.posted_at else float("-inf")),
         -item.final_score)
        for item in results
    ]
    assert keys == sorted(keys), (
        "results are not ordered newest-first with score as the tie-break: "
        f"{[(str(i.posted_at), i.final_score) for i in results]}"
    )


def assert_recency_outranks_score(results) -> None:
    """At least one newer-but-weaker job precedes an older-but-stronger one.

    Without this the suite would still pass if ranking quietly reverted to
    score-first and the fixture happened to be newest-first already.
    """
    dated = [i for i in results if i.posted_at is not None]
    for earlier, later in ((a, b) for x, a in enumerate(dated) for b in dated[x + 1:]):
        if earlier.final_score < later.final_score and earlier.posted_at > later.posted_at:
            return
    raise AssertionError(
        "no newer-lower-scoring job ranked above an older-higher-scoring one, "
        "so this result set does not demonstrate the ranking contract"
    )


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
    assert_ranked_newest_first(outcome.results)


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

    # Phase 12: no portal is given a share of the request. Each searches to
    # its own depth and the returned set is the Top N of the combined pool.
    assert [leg.target_quota for leg in prepared.legs] == [20, 20]
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


# --- ZipRecruiter: collection works, qualification cannot -------------------
#
# The surface ZipRecruiter permits this client to read publishes no posting
# description. The matching engine rejects a posting it cannot read, so these
# tests assert what actually happens rather than a result the data cannot
# support. Padding the description to clear that gate is exactly what the
# product forbids.


async def test_a_ziprecruiter_only_search_collects_but_qualifies_nothing(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("ziprecruiter", build_ziprecruiter())]
    )

    # A completed run that found listings and honestly rejected all of them is
    # not a failure: it is the 70% threshold doing its job.
    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count == len(ziprecruiter_records())
    assert prepared.legs[0].qualified_count == 0
    assert outcome.results == []


async def matches_of(session: AsyncSession, run_id) -> list[JobMatch]:
    """Scored rows belonging to one run.

    Scoped deliberately: this database also holds the operator's own data, so
    an unscoped ``select(JobMatch)`` would assert against their rows too.
    """
    return list(
        (
            await session.scalars(
                select(JobMatch).where(JobMatch.search_run_id == run_id)
            )
        ).all()
    )


async def jobs_of(session: AsyncSession, run_id) -> list[Job]:
    """Canonical jobs discovered or rediscovered by one run."""
    job_ids = [match.job_id for match in await matches_of(session, run_id)]
    if not job_ids:
        return []
    return list(
        (await session.scalars(select(Job).where(Job.id.in_(job_ids)))).all()
    )


async def test_the_rejection_reason_names_the_missing_description(
    db_session: AsyncSession,
) -> None:
    # The user is told why, not just that nothing came back.
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("ziprecruiter", build_ziprecruiter())]
    )

    matches = await matches_of(db_session, prepared.run.id)

    assert matches
    assert all(not match.qualified for match in matches)
    reasons = [
        str(reason).lower()
        for match in matches
        for reason in (match.rejection_reasons or [])
    ]
    assert any("description" in reason for reason in reasons)


async def test_ziprecruiter_listings_still_normalise_and_persist(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejected for scoring is not the same as discarded.

    The listings are canonicalised and stored with their provenance, so the
    moment a description source exists they are scoreable without re-fetching.

    ZipRecruiter publishes ``days_ago`` rather than a date, so its timestamps
    are relative to whenever the adapter runs. ``PortalSearchRequest`` carries
    no clock - adapters read the wall clock, which is the contract - while the
    run itself is frozen at NOW. Left alone, the two disagree by however long
    ago this fixture was recorded, the dates land in the future, and
    ``normalize_posted_at`` correctly discards them. Freezing the adapter's
    clock to the same NOW is what makes a relative-date portal testable at all;
    without it this passes only while the wall clock sits near NOW.
    """
    monkeypatch.setattr(ziprecruiter_module, "datetime", _FrozenDatetime)

    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("ziprecruiter", build_ziprecruiter())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    assert len(jobs) == len(ziprecruiter_records())
    assert all(job.company for job in jobs)
    assert all(job.posted_at is not None for job in jobs)

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(
                    JobSource.job_id.in_([job.id for job in jobs])
                )
            )
        ).all()
    )
    assert len(sources) == len(jobs)


async def test_salary_and_work_arrangement_survive_in_the_stored_payload(
    db_session: AsyncSession,
) -> None:
    """Fields the shared contract has no room for are still kept verbatim.

    RawJob carries no salary and no work-mode field, so without this a later
    scoring version would have to re-query the portal to see them.
    """
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("ziprecruiter", build_ziprecruiter())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    captures = list(
        (
            await db_session.scalars(
                select(RawJobRecord).where(
                    RawJobRecord.job_id.in_([job.id for job in jobs])
                )
            )
        ).all()
    )

    assert captures
    assert all(capture.source == "ziprecruiter" for capture in captures)
    assert any("salary" in capture.raw_payload for capture in captures)
    assert any("job_type" in capture.raw_payload for capture in captures)
    # And the description really is absent, not merely unread.
    assert all(capture.description is None for capture in captures)


# --- ZipRecruiter alongside the other two ----------------------------------


async def test_hiring_cafe_still_returns_its_jobs_with_ziprecruiter_selected(
    db_session: AsyncSession,
) -> None:
    # A portal that qualifies nothing must not cost the others their results.
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe()),
            ("ziprecruiter", build_ziprecruiter()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert outcome.results
    assert all(carried_by(item, "hiringcafe") for item in outcome.results)
    assert all(item.final_score >= 70.0 for item in outcome.results)


async def test_jobright_is_unaffected_by_ziprecruiter_being_selected(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    qualifying = _mirror_as_jobright(hiringcafe_records()[0])

    # Distinct portal rows per run: the slug is unique, and this test runs the
    # same search twice on purpose.
    _, alone = await run_search(
        db_session, resume=resume, specs=[("jr-alone", build_jobright([qualifying]))]
    )
    _, together = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jr-together", build_jobright([qualifying])),
            ("zr-together", build_ziprecruiter()),
        ],
    )

    assert [item.final_score for item in alone.results] == [
        item.final_score for item in together.results
    ]


async def test_all_three_portals_run_in_one_search(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("ziprecruiter", build_ziprecruiter()),
        ],
        limit=20,
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 3
    # Every selected portal is searched; none is skipped once another fills up.
    assert all(leg.raw_count > 0 for leg in prepared.legs)


async def test_three_portal_quotas_are_allocated_evenly(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("ziprecruiter", build_ziprecruiter()),
        ],
        limit=20,
    )

    # Phase 12: no portal is capped at limit / portal_count. Every leg may in
    # principle supply the whole request; what it contributes is decided by the
    # ranking afterwards.
    assert [leg.target_quota for leg in outcome.portals] == [20, 20, 20]
    assert sum(leg.selected_count for leg in outcome.portals) <= 20


async def test_the_three_portal_result_is_ranked_and_free_of_duplicates(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("ziprecruiter", build_ziprecruiter()),
        ],
    )

    assert_ranked_newest_first(outcome.results)

    job_ids = [item.job_id for item in outcome.results]
    assert len(job_ids) == len(set(job_ids))


# --- Cross-portal deduplication involving ZipRecruiter ----------------------


def _match_token(key: str) -> str:
    """Build a listing token carrying ``key``, the way the live one is built.

    Exercises the real decoder rather than stubbing around it.
    """
    import base64

    inner = b"\n" + bytes([len(key)]) + key.encode() + b"\x12$" + b"0" * 36
    outer = b"\n" + bytes([len(inner)]) + inner
    return base64.urlsafe_b64encode(outer).decode().rstrip("=")


def _mirror_as_ziprecruiter(
    record: dict, *, key: str = "zrMirrorKey0000000001"
) -> dict:
    """Recast one Hiring Cafe listing as the same vacancy on ZipRecruiter."""
    processed = record["v5_processed_job_data"]
    return {
        "title": record["job_information"]["title"],
        "company": record["enriched_company_data"]["name"],
        "location": processed.get("formatted_workplace_location"),
        "is_remote": False,
        "salary": {"min_annual": 70000, "max_annual": 95000},
        "company_logo": None,
        "job_redirect_url": (
            "https://www.ziprecruiter.com/job-redirect?match_token="
            + _match_token(key)
        ),
        "job_type": "Full-time · On-site",
        "benefits": "Medical, Dental",
        "days_ago": 1,
    }


async def test_one_vacancy_on_hiring_cafe_and_ziprecruiter_is_one_job(
    db_session: AsyncSession,
) -> None:
    """The merged job keeps both sources - and stays scoreable.

    ZipRecruiter brings no description, so on its own this vacancy could not
    be scored. Merged onto the Hiring Cafe record it is, and ZipRecruiter's
    provenance is still recorded against it.
    """
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe([shared])),
            ("ziprecruiter", build_ziprecruiter([_mirror_as_ziprecruiter(shared)])),
        ],
    )

    company = shared["enriched_company_data"]["name"]
    jobs = [
        job
        for job in (await db_session.scalars(select(Job))).all()
        if job.company == company
    ]
    assert len(jobs) == 1, [job.title for job in jobs]

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == jobs[0].id)
            )
        ).all()
    )
    assert len(sources) == 2

    item = next(item for item in outcome.results if item.job_id == jobs[0].id)
    assert carried_by(item, "hiringcafe")
    assert carried_by(item, "ziprecruiter")


async def test_the_employer_url_wins_over_the_ziprecruiter_board_link(
    db_session: AsyncSession,
) -> None:
    # CLAUDE.md section 9: employer career page, then ATS, then portal listing.
    # A ziprecruiter.com link must never displace an employer application URL.
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe([shared])),
            ("ziprecruiter", build_ziprecruiter([_mirror_as_ziprecruiter(shared)])),
        ],
    )

    assert outcome.results
    item = outcome.results[0]
    assert "ziprecruiter.com" not in item.preferred_application_url
    assert item.preferred_url_kind in {
        JobUrlKind.ATS.value,
        JobUrlKind.EMPLOYER_CAREER.value,
    }


async def test_rediscovering_a_ziprecruiter_listing_creates_no_second_job(
    db_session: AsyncSession,
) -> None:
    """The listing URL rotates per request; the job key inside it does not.

    Without that key the same vacancy would be filed as new on every search.
    """
    _, resume = await analysed_resume(db_session)
    record = _mirror_as_ziprecruiter(hiringcafe_records()[0])
    company = record["company"]

    async def stored_for_this_company() -> list[Job]:
        return list(
            (
                await db_session.scalars(select(Job).where(Job.company == company))
            ).all()
        )

    first_prepared, _ = await run_search(
        db_session,
        resume=resume,
        specs=[("zr-first", build_ziprecruiter([record]))],
    )
    after_first = await stored_for_this_company()

    # Same vacancy, freshly issued token - exactly what a re-search returns.
    rotated = dict(record)
    rotated["job_redirect_url"] = (
        "https://www.ziprecruiter.com/job-redirect?match_token="
        + _match_token("zrMirrorKey0000000001")
        + "&tsid=100000500"
    )
    second_prepared, _ = await run_search(
        db_session,
        resume=resume,
        specs=[("zr-second", build_ziprecruiter([rotated]))],
    )
    after_second = await stored_for_this_company()

    assert len(after_first) == 1
    # The rotated token must not file the same vacancy a second time.
    assert len(after_second) == 1
    assert after_first[0].id == after_second[0].id
    assert first_prepared.run.id != second_prepared.run.id


# --- Failure isolation, in every direction ---------------------------------


async def test_ziprecruiter_failing_still_returns_the_other_portals_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe()),
            (
                "ziprecruiter",
                build_ziprecruiter(
                    failure=PortalTimeoutError("timed out", portal="ziprecruiter")
                ),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.results
    assert all(carried_by(item, "hiringcafe") for item in outcome.results)

    failed = [leg for leg in outcome.portals if leg.failed]
    assert len(failed) == 1
    assert failed[0].portal_slug.endswith("ziprecruiter")
    assert failed[0].error


async def test_another_portal_failing_does_not_stop_ziprecruiter_collecting(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "hiringcafe",
                build_hiringcafe(
                    failure=PortalParseError("bad page", portal="hiringcafe")
                ),
            ),
            ("ziprecruiter", build_ziprecruiter()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS

    by_slug = {leg.portal_slug.rsplit("-", 1)[-1]: leg for leg in outcome.portals}
    assert by_slug["hiringcafe"].failed
    # It still ran and still collected, even though nothing it found qualified.
    assert not by_slug["ziprecruiter"].failed
    assert by_slug["ziprecruiter"].collected_count > 0


async def test_all_three_failing_fails_the_run_with_every_error_recorded(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "jobright",
                build_jobright(failure=PortalTimeoutError("t", portal="jobright")),
            ),
            (
                "hiringcafe",
                build_hiringcafe(failure=PortalParseError("p", portal="hiringcafe")),
            ),
            (
                "ziprecruiter",
                build_ziprecruiter(
                    failure=PortalTimeoutError("t", portal="ziprecruiter")
                ),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.FAILED
    assert outcome.results == []
    assert all(leg.failed for leg in outcome.portals)
    assert all(leg.error for leg in outcome.portals)


# --- SimplyHired through the real pipeline ---------------------------------


async def test_a_simplyhired_only_search_returns_its_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("simplyhired", build_simplyhired())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count > 0
    assert outcome.results, "the composed descriptions qualified nothing at all"
    assert all(item.final_score >= 70.0 for item in outcome.results)
    assert all(carried_by(item, "simplyhired") for item in outcome.results)


async def test_simplyhired_listings_normalise_with_their_provenance(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("simplyhired", build_simplyhired())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    assert jobs
    assert all(job.company for job in jobs)
    assert all(job.posted_at is not None for job in jobs)

    captures = list(
        (
            await db_session.scalars(
                select(RawJobRecord).where(
                    RawJobRecord.job_id.in_([job.id for job in jobs])
                )
            )
        ).all()
    )
    assert captures
    assert all(capture.source == "simplyhired" for capture in captures)
    # Fields the shared contract has no room for are kept verbatim.
    assert any("salaryInfo" in capture.raw_payload for capture in captures)
    assert any("remoteAttributes" in capture.raw_payload for capture in captures)


async def test_every_returned_simplyhired_job_gives_the_user_a_usable_link(
    db_session: AsyncSession,
) -> None:
    # The disallowed /out?r= redirect must never reach the dashboard.
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("simplyhired", build_simplyhired())]
    )

    assert outcome.results
    for item in outcome.results:
        assert item.preferred_application_url.startswith("http")
        assert "/out?r=" not in item.preferred_application_url


async def test_a_remote_listing_keeps_its_work_mode(
    db_session: AsyncSession,
) -> None:
    """The site puts "Remote" in the location field, so normalisation reads it.

    No adapter-side invention needed, and the location key stays clean for
    cross-portal grouping.
    """
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("simplyhired", build_simplyhired())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)

    assert any(job.work_mode is not None for job in jobs)


# --- SimplyHired beside each existing portal -------------------------------


async def test_jobright_plus_simplyhired_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("simplyhired", build_simplyhired()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 2
    assert all(leg.raw_count > 0 for leg in prepared.legs)
    assert any(carried_by(item, "simplyhired") for item in outcome.results)


async def test_hiring_cafe_plus_simplyhired_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert any(carried_by(item, "hiringcafe") for item in outcome.results)
    assert any(carried_by(item, "simplyhired") for item in outcome.results)


async def test_hiring_cafe_is_unaffected_by_simplyhired_being_selected(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, alone = await run_search(
        db_session, resume=resume, specs=[("hc-alone", build_hiringcafe())]
    )
    _, together = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hc-together", build_hiringcafe()),
            ("sh-together", build_simplyhired()),
        ],
        limit=50,
    )

    alone_scores = {i.job_id: i.final_score for i in alone.results}
    together_scores = {i.job_id: i.final_score for i in together.results}
    overlap = set(alone_scores) & set(together_scores)

    assert overlap
    assert all(alone_scores[j] == together_scores[j] for j in overlap)


async def test_all_operational_portals_run_in_one_search(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
        ],
        limit=20,
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 3
    # Every selected portal is searched; none is skipped once another fills up.
    assert all(leg.raw_count > 0 for leg in prepared.legs)


async def test_quotas_are_shared_across_the_three_operational_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
        ],
        limit=20,
    )

    assert [leg.target_quota for leg in outcome.portals] == [20, 20, 20]
    assert sum(leg.selected_count for leg in outcome.portals) <= 20


async def test_the_three_portal_result_is_ranked_and_free_of_duplicates(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
        ],
    )

    assert_ranked_newest_first(outcome.results)

    job_ids = [item.job_id for item in outcome.results]
    assert len(job_ids) == len(set(job_ids))
    assert all(item.final_score >= 70.0 for item in outcome.results)


# --- Cross-portal deduplication involving SimplyHired ----------------------


def _mirror_as_simplyhired(record: dict, *, key: str = "shMirrorKey000001") -> dict:
    """Recast one Hiring Cafe listing as the same vacancy on SimplyHired."""
    processed = record["v5_processed_job_data"]
    summary = processed.get("requirements_summary") or ""
    return {
        "jobKey": key,
        "title": record["job_information"]["title"],
        "company": record["enriched_company_data"]["name"],
        "location": processed.get("formatted_workplace_location"),
        "snippet": summary[:150],
        "requirements": list(processed.get("technical_tools") or [])[:8],
        "jobTypes": ["Full-time"],
        "benefits": ["Health insurance"],
        "remoteAttributes": [],
        "uncategorized": list(processed.get("role_activities") or [])[:20],
        "salaryInfo": "$80,000 - $95,000 a year",
        "dateOnIndeed": 1784922724873,
        "botUrl": f"/job/{key}",
    }


async def test_one_vacancy_on_hiring_cafe_and_simplyhired_is_one_job(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe([shared])),
            ("simplyhired", build_simplyhired([_mirror_as_simplyhired(shared)])),
        ],
    )

    company = shared["enriched_company_data"]["name"]
    jobs = [
        job
        for job in (await db_session.scalars(select(Job))).all()
        if job.company == company
    ]
    assert len(jobs) == 1, [job.title for job in jobs]

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == jobs[0].id)
            )
        ).all()
    )
    assert len(sources) == 2

    item = next(item for item in outcome.results if item.job_id == jobs[0].id)
    assert carried_by(item, "hiringcafe")
    assert carried_by(item, "simplyhired")


async def test_one_vacancy_on_all_three_portals_is_still_one_job(
    db_session: AsyncSession,
) -> None:
    """The requirement stated for Phase 9C, exercised directly.

    One canonical job, three sources, provenance preserved for each.
    """
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright([_mirror_as_jobright(shared)])),
            ("hiringcafe", build_hiringcafe([shared])),
            ("simplyhired", build_simplyhired([_mirror_as_simplyhired(shared)])),
        ],
    )

    company = shared["enriched_company_data"]["name"]
    jobs = [
        job
        for job in (await db_session.scalars(select(Job))).all()
        if job.company == company
    ]
    assert len(jobs) == 1, [job.title for job in jobs]

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == jobs[0].id)
            )
        ).all()
    )
    assert len(sources) == 3

    item = next(item for item in outcome.results if item.job_id == jobs[0].id)
    assert len(item.portal_slugs) == 3
    for slug in ("jobright", "hiringcafe", "simplyhired"):
        assert carried_by(item, slug)


async def test_the_employer_url_wins_over_the_simplyhired_listing(
    db_session: AsyncSession,
) -> None:
    # CLAUDE.md section 9: employer career page, then ATS, then portal listing.
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe([shared])),
            ("simplyhired", build_simplyhired([_mirror_as_simplyhired(shared)])),
        ],
    )

    assert outcome.results
    item = outcome.results[0]
    assert "simplyhired.com" not in item.preferred_application_url
    assert item.preferred_url_kind in {
        JobUrlKind.ATS.value,
        JobUrlKind.EMPLOYER_CAREER.value,
    }


async def test_a_duplicate_across_three_portals_takes_one_quota_slot(
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
            ("simplyhired", build_simplyhired([_mirror_as_simplyhired(shared)])),
        ],
    )

    # One vacancy, however many portals carried it.
    assert len(outcome.results) == 1


async def test_rediscovering_a_simplyhired_listing_creates_no_second_job(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    record = _mirror_as_simplyhired(hiringcafe_records()[0])
    company = record["company"]

    async def stored_for_this_company() -> list[Job]:
        return list(
            (
                await db_session.scalars(select(Job).where(Job.company == company))
            ).all()
        )

    await run_search(
        db_session,
        resume=resume,
        specs=[("sh-first", build_simplyhired([record]))],
    )
    after_first = await stored_for_this_company()

    await run_search(
        db_session,
        resume=resume,
        specs=[("sh-second", build_simplyhired([dict(record)]))],
    )
    after_second = await stored_for_this_company()

    assert len(after_first) == 1
    assert len(after_second) == 1
    assert after_first[0].id == after_second[0].id


async def test_repeating_a_three_portal_search_is_stable(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, first = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jr-1", build_jobright()),
            ("hc-1", build_hiringcafe()),
            ("sh-1", build_simplyhired()),
        ],
    )
    await db_session.commit()

    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jr-2", build_jobright()),
            ("hc-2", build_hiringcafe()),
            ("sh-2", build_simplyhired()),
        ],
    )

    assert [i.job_id for i in second.results] == [i.job_id for i in first.results]
    assert [i.final_score for i in second.results] == [
        i.final_score for i in first.results
    ]


# --- Failure isolation, in every direction ---------------------------------


async def test_simplyhired_failing_still_returns_the_other_portals_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe()),
            (
                "simplyhired",
                build_simplyhired(
                    failure=PortalTimeoutError("timed out", portal="simplyhired")
                ),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.results
    assert all(carried_by(item, "hiringcafe") for item in outcome.results)

    failed = [leg for leg in outcome.portals if leg.failed]
    assert len(failed) == 1
    assert failed[0].portal_slug.endswith("simplyhired")
    assert failed[0].error


async def test_another_portal_failing_does_not_stop_simplyhired(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "hiringcafe",
                build_hiringcafe(
                    failure=PortalParseError("bad page", portal="hiringcafe")
                ),
            ),
            ("simplyhired", build_simplyhired()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.results
    assert all(carried_by(item, "simplyhired") for item in outcome.results)

    by_slug = {leg.portal_slug.rsplit("-", 1)[-1]: leg for leg in outcome.portals}
    assert by_slug["hiringcafe"].failed
    assert not by_slug["simplyhired"].failed


async def test_two_of_three_failing_still_completes_with_errors(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "jobright",
                build_jobright(failure=PortalTimeoutError("t", portal="jobright")),
            ),
            (
                "hiringcafe",
                build_hiringcafe(failure=PortalParseError("p", portal="hiringcafe")),
            ),
            ("simplyhired", build_simplyhired()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    # The surviving portal still delivers.
    assert outcome.results
    assert len([leg for leg in outcome.portals if leg.failed]) == 2


async def test_all_three_failing_fails_the_run_with_every_error_recorded(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "jobright",
                build_jobright(failure=PortalTimeoutError("t", portal="jobright")),
            ),
            (
                "hiringcafe",
                build_hiringcafe(failure=PortalParseError("p", portal="hiringcafe")),
            ),
            (
                "simplyhired",
                build_simplyhired(
                    failure=PortalTimeoutError("t", portal="simplyhired")
                ),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.FAILED
    assert outcome.results == []
    assert all(leg.failed for leg in outcome.portals)
    assert all(leg.error for leg in outcome.portals)


# --- Eligibility is unchanged ----------------------------------------------


async def test_an_applied_job_stays_excluded_when_simplyhired_finds_it(
    db_session: AsyncSession,
) -> None:
    """Applied status is keyed to the canonical job, not to the portal.

    A new portal rediscovering a vacancy must not resurface it as a fresh
    opportunity.
    """
    shared = hiringcafe_records()[0]
    user, resume = await analysed_resume(db_session)

    _, first = await run_search(
        db_session, resume=resume, specs=[("hc-first", build_hiringcafe([shared]))]
    )
    assert first.results
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=ApplicationStatus.APPLIED,
            applied_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[("sh-later", build_simplyhired([_mirror_as_simplyhired(shared)]))],
    )

    assert job_id not in {item.job_id for item in second.results}


# --- Greenhouse through the real pipeline ----------------------------------
#
# The captured fixture holds real Greenhouse postings, and none of them clears
# 70 for the resume these tests build: Stripe's asks for seven years against
# its five, and Instacart's two are Senior roles against a mid-level resume.
# They score 52.15 and 60.85, which is the threshold working, not a defect.
#
# Complete employer prose is *harder* to match than a composed summary, because
# it states requirements a five-skill test resume does not evidence. So tests
# that need a qualifying result use a vacancy recast into Greenhouse's payload
# shape, and the honest zero is asserted directly below.


def qualifying_greenhouse(company: str | None = None) -> list[dict]:
    """A Greenhouse posting that does clear the threshold for this resume."""
    record = _mirror_as_greenhouse(hiringcafe_records()[0])
    if company is not None:
        record["company_name"] = company
    return [record]


async def test_a_greenhouse_only_search_collects_and_scores_every_posting(
    db_session: AsyncSession,
) -> None:
    """The real fixture qualifies nothing, and that is reported honestly.

    Lowering the threshold to make this look better is exactly what the
    product forbids.
    """
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count == 3
    assert prepared.legs[0].qualified_count == 0
    assert outcome.results == []

    matches = await matches_of(db_session, prepared.run.id)
    assert matches, "the postings were never scored at all"
    assert all(not match.qualified for match in matches)
    # Rejected on the merits, not for being unreadable.
    reasons = [
        str(reason).lower()
        for match in matches
        for reason in (match.rejection_reasons or [])
    ]
    assert not any("description" in reason for reason in reasons)


async def test_a_qualifying_greenhouse_posting_is_returned(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("greenhouse", build_greenhouse(qualifying_greenhouse()))],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert outcome.results
    assert all(item.final_score >= 70.0 for item in outcome.results)
    assert all(carried_by(item, "greenhouse") for item in outcome.results)


async def test_greenhouse_jobs_carry_the_complete_posting_text(
    db_session: AsyncSession,
) -> None:
    """The whole point of this portal: the employer's own words.

    No composed summary, no truncation, no second fetch.
    """
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    assert jobs
    for job in jobs:
        assert job.description
        assert len(job.description) > 500
        # Markup must never reach the scored text or the dashboard.
        assert "<p>" not in job.description
        assert "&lt;" not in job.description


async def test_greenhouse_listings_normalise_with_their_provenance(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    assert jobs
    assert all(job.company for job in jobs)
    assert all(job.posted_at is not None for job in jobs)

    captures = list(
        (
            await db_session.scalars(
                select(RawJobRecord).where(
                    RawJobRecord.job_id.in_([job.id for job in jobs])
                )
            )
        ).all()
    )
    assert captures
    assert all(capture.source == "greenhouse" for capture in captures)
    assert all("absolute_url" in capture.raw_payload for capture in captures)


async def test_every_returned_greenhouse_job_gives_the_user_a_usable_link(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("greenhouse", build_greenhouse(qualifying_greenhouse()))],
    )

    assert outcome.results
    for item in outcome.results:
        assert item.preferred_application_url.startswith("http")


async def test_an_employer_hosted_link_outranks_a_board_link(
    db_session: AsyncSession,
) -> None:
    """Greenhouse boards may point at the employer's own careers page.

    When they do, the existing preference rules must surface that rather than
    a greenhouse.io URL (CLAUDE.md section 9).
    """
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("greenhouse", build_greenhouse(qualifying_greenhouse()))],
    )

    kinds = {item.preferred_url_kind for item in outcome.results}
    # Every link is at least an ATS destination; none falls back to unknown.
    assert kinds
    assert JobUrlKind.UNKNOWN.value not in kinds


# --- Greenhouse beside each existing portal --------------------------------


async def test_jobright_plus_greenhouse_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("greenhouse", build_greenhouse(qualifying_greenhouse())),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 2
    assert all(leg.raw_count > 0 for leg in prepared.legs)
    assert any(carried_by(item, "greenhouse") for item in outcome.results)


async def test_hiring_cafe_plus_greenhouse_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe()),
            (
                "greenhouse",
                build_greenhouse(qualifying_greenhouse(company="Northwind Trading")),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert any(carried_by(item, "hiringcafe") for item in outcome.results)
    assert any(carried_by(item, "greenhouse") for item in outcome.results)


async def test_simplyhired_plus_greenhouse_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("simplyhired", build_simplyhired()),
            (
                "greenhouse",
                build_greenhouse(qualifying_greenhouse(company="Northwind Trading")),
            ),
        ],
        limit=50,
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert any(carried_by(item, "simplyhired") for item in outcome.results)
    assert any(carried_by(item, "greenhouse") for item in outcome.results)


async def test_existing_portals_are_unaffected_by_greenhouse_being_selected(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, alone = await run_search(
        db_session, resume=resume, specs=[("hc-alone", build_hiringcafe())]
    )
    _, together = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hc-together", build_hiringcafe()),
            ("gh-together", build_greenhouse()),
        ],
        limit=50,
    )

    alone_scores = {i.job_id: i.final_score for i in alone.results}
    together_scores = {i.job_id: i.final_score for i in together.results}
    overlap = set(alone_scores) & set(together_scores)

    assert overlap
    assert all(alone_scores[j] == together_scores[j] for j in overlap)


async def test_all_four_operational_portals_run_in_one_search(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
            ("greenhouse", build_greenhouse()),
        ],
        limit=20,
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 4
    # Every selected portal is searched; none is skipped once another fills up.
    assert all(leg.raw_count > 0 for leg in prepared.legs)


async def test_quotas_are_shared_across_four_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
            ("greenhouse", build_greenhouse()),
        ],
        limit=20,
    )

    # Phase 12: no portal is capped at limit / portal_count.
    assert [leg.target_quota for leg in outcome.portals] == [20, 20, 20, 20]
    assert sum(leg.selected_count for leg in outcome.portals) <= 20


async def test_the_four_portal_result_is_ranked_and_free_of_duplicates(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
            ("greenhouse", build_greenhouse()),
        ],
    )

    assert_ranked_newest_first(outcome.results)

    job_ids = [item.job_id for item in outcome.results]
    assert len(job_ids) == len(set(job_ids))
    assert all(item.final_score >= 70.0 for item in outcome.results)


# --- Cross-portal deduplication involving Greenhouse -----------------------


def _mirror_as_greenhouse(record: dict, *, job_id: int = 99000001) -> dict:
    """Recast one Hiring Cafe listing as the same vacancy on Greenhouse."""
    processed = record["v5_processed_job_data"]
    summary = processed.get("requirements_summary") or ""
    activities = processed.get("role_activities") or []
    body = (
        "<h2>About the role</h2><p>"
        + summary
        + "</p><h3>Responsibilities</h3><ul>"
        + "".join(f"<li>{item}</li>" for item in activities)
        + "</ul>"
    )
    escaped = (
        body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return {
        "id": job_id,
        "title": record["job_information"]["title"],
        "company_name": record["enriched_company_data"]["name"],
        "location": {"name": processed.get("formatted_workplace_location")},
        "absolute_url": f"https://boards.greenhouse.io/mirror/jobs/{job_id}",
        "content": escaped,
        "first_published": "2026-07-30T20:41:50-04:00",
        "updated_at": "2026-07-30T20:41:50-04:00",
        "metadata": None,
    }


async def test_one_vacancy_on_hiring_cafe_and_greenhouse_is_one_job(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe([shared])),
            ("greenhouse", build_greenhouse([_mirror_as_greenhouse(shared)])),
        ],
    )

    company = shared["enriched_company_data"]["name"]
    jobs = [
        job
        for job in (await db_session.scalars(select(Job))).all()
        if job.company == company
    ]
    assert len(jobs) == 1, [job.title for job in jobs]

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == jobs[0].id)
            )
        ).all()
    )
    assert len(sources) == 2

    item = next(item for item in outcome.results if item.job_id == jobs[0].id)
    assert carried_by(item, "hiringcafe")
    assert carried_by(item, "greenhouse")


async def test_one_vacancy_on_all_four_portals_is_still_one_job(
    db_session: AsyncSession,
) -> None:
    """The requirement stated for Phase 9D, exercised directly.

    One canonical job, four sources, provenance preserved for each.
    """
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright([_mirror_as_jobright(shared)])),
            ("hiringcafe", build_hiringcafe([shared])),
            ("simplyhired", build_simplyhired([_mirror_as_simplyhired(shared)])),
            ("greenhouse", build_greenhouse([_mirror_as_greenhouse(shared)])),
        ],
    )

    company = shared["enriched_company_data"]["name"]
    jobs = [
        job
        for job in (await db_session.scalars(select(Job))).all()
        if job.company == company
    ]
    assert len(jobs) == 1, [job.title for job in jobs]

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == jobs[0].id)
            )
        ).all()
    )
    assert len(sources) == 4

    item = next(item for item in outcome.results if item.job_id == jobs[0].id)
    assert len(item.portal_slugs) == 4
    for slug in ("jobright", "hiringcafe", "simplyhired", "greenhouse"):
        assert carried_by(item, slug)


async def test_a_duplicate_across_four_portals_takes_one_quota_slot(
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
            ("simplyhired", build_simplyhired([_mirror_as_simplyhired(shared)])),
            ("greenhouse", build_greenhouse([_mirror_as_greenhouse(shared)])),
        ],
    )

    # One vacancy, however many portals carried it.
    assert len(outcome.results) == 1


async def test_the_employer_ats_url_wins_over_the_board_listing(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe([shared])),
            ("greenhouse", build_greenhouse([_mirror_as_greenhouse(shared)])),
        ],
    )

    assert outcome.results
    item = outcome.results[0]
    assert item.preferred_url_kind in {
        JobUrlKind.ATS.value,
        JobUrlKind.EMPLOYER_CAREER.value,
    }


async def test_rediscovering_a_greenhouse_posting_creates_no_second_job(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    record = _mirror_as_greenhouse(hiringcafe_records()[0])
    company = record["company_name"]

    async def stored_for_this_company() -> list[Job]:
        return list(
            (
                await db_session.scalars(select(Job).where(Job.company == company))
            ).all()
        )

    await run_search(
        db_session,
        resume=resume,
        specs=[("gh-first", build_greenhouse([record]))],
    )
    after_first = await stored_for_this_company()

    await run_search(
        db_session,
        resume=resume,
        specs=[("gh-second", build_greenhouse([dict(record)]))],
    )
    after_second = await stored_for_this_company()

    assert len(after_first) == 1
    assert len(after_second) == 1
    assert after_first[0].id == after_second[0].id


async def test_repeating_a_four_portal_search_is_stable(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, first = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jr-1", build_jobright()),
            ("hc-1", build_hiringcafe()),
            ("sh-1", build_simplyhired()),
            ("gh-1", build_greenhouse()),
        ],
    )
    await db_session.commit()

    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jr-2", build_jobright()),
            ("hc-2", build_hiringcafe()),
            ("sh-2", build_simplyhired()),
            ("gh-2", build_greenhouse()),
        ],
    )

    assert [i.job_id for i in second.results] == [i.job_id for i in first.results]
    assert [i.final_score for i in second.results] == [
        i.final_score for i in first.results
    ]


# --- Failure isolation, in every direction ---------------------------------


async def test_greenhouse_failing_still_returns_the_other_portals_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe()),
            (
                "greenhouse",
                build_greenhouse(
                    failure=PortalTimeoutError("timed out", portal="greenhouse")
                ),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.results
    assert all(carried_by(item, "hiringcafe") for item in outcome.results)

    failed = [leg for leg in outcome.portals if leg.failed]
    assert len(failed) == 1
    assert failed[0].portal_slug.endswith("greenhouse")
    assert failed[0].error


async def test_another_portal_failing_does_not_stop_greenhouse(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "hiringcafe",
                build_hiringcafe(
                    failure=PortalParseError("bad page", portal="hiringcafe")
                ),
            ),
            ("greenhouse", build_greenhouse(qualifying_greenhouse())),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.results
    assert all(carried_by(item, "greenhouse") for item in outcome.results)

    by_slug = {leg.portal_slug.rsplit("-", 1)[-1]: leg for leg in outcome.portals}
    assert by_slug["hiringcafe"].failed
    assert not by_slug["greenhouse"].failed


async def test_three_of_four_failing_still_completes_with_errors(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "jobright",
                build_jobright(failure=PortalTimeoutError("t", portal="jobright")),
            ),
            (
                "hiringcafe",
                build_hiringcafe(failure=PortalParseError("p", portal="hiringcafe")),
            ),
            (
                "simplyhired",
                build_simplyhired(
                    failure=PortalTimeoutError("t", portal="simplyhired")
                ),
            ),
            ("greenhouse", build_greenhouse(qualifying_greenhouse())),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    # The surviving portal still delivers.
    assert outcome.results
    assert all(carried_by(item, "greenhouse") for item in outcome.results)
    assert len([leg for leg in outcome.portals if leg.failed]) == 3


async def test_all_four_failing_fails_the_run_with_every_error_recorded(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "jobright",
                build_jobright(failure=PortalTimeoutError("t", portal="jobright")),
            ),
            (
                "hiringcafe",
                build_hiringcafe(failure=PortalParseError("p", portal="hiringcafe")),
            ),
            (
                "simplyhired",
                build_simplyhired(
                    failure=PortalTimeoutError("t", portal="simplyhired")
                ),
            ),
            (
                "greenhouse",
                build_greenhouse(
                    failure=PortalTimeoutError("t", portal="greenhouse")
                ),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.FAILED
    assert outcome.results == []
    assert all(leg.failed for leg in outcome.portals)
    assert all(leg.error for leg in outcome.portals)


# --- Eligibility is unchanged ----------------------------------------------


async def test_an_applied_job_stays_excluded_when_greenhouse_finds_it(
    db_session: AsyncSession,
) -> None:
    """Applied status is keyed to the canonical job, not to the portal."""
    shared = hiringcafe_records()[0]
    user, resume = await analysed_resume(db_session)

    _, first = await run_search(
        db_session, resume=resume, specs=[("hc-first", build_hiringcafe([shared]))]
    )
    assert first.results
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=ApplicationStatus.APPLIED,
            applied_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[("gh-later", build_greenhouse([_mirror_as_greenhouse(shared)]))],
    )

    assert job_id not in {item.job_id for item in second.results}


# --- Scale.jobs through the real pipeline ----------------------------------
#
# The captured fixture holds twenty real Scale.jobs listings. Six clear 70 for
# the resume these tests build, and the twenty records collapse to fourteen
# canonical jobs: the endpoint really does publish the same vacancy more than
# once, so this portal exercises in-portal deduplication with real data rather
# than a constructed pair.
#
# Descriptions here are the publisher's model-written summaries, not employer
# prose, so they run to roughly 300-600 characters where Greenhouse's run to
# thousands. That is stated in the adapter and asserted below rather than
# glossed over.


async def test_a_scalejobs_only_search_returns_its_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("scalejobs", build_scalejobs())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count == 20
    assert prepared.legs[0].qualified_count == 6
    assert outcome.results
    assert all(item.final_score >= 70.0 for item in outcome.results)
    assert all(carried_by(item, "scalejobs") for item in outcome.results)


async def test_the_publisher_repeats_a_vacancy_and_it_becomes_one_job(
    db_session: AsyncSession,
) -> None:
    """Twenty real listings, fourteen real vacancies.

    The endpoint publishes the same requisition under more than one record.
    Canonicalisation is what collapses them, not the adapter.
    """
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("scalejobs", build_scalejobs())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    assert prepared.legs[0].raw_count == 20
    assert len(jobs) == 14

    keys = [job.canonical_key for job in jobs]
    assert len(keys) == len(set(keys))


async def test_scalejobs_listings_normalise_with_their_provenance(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("scalejobs", build_scalejobs())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    assert jobs
    assert all(job.company for job in jobs)
    assert all(job.posted_at is not None for job in jobs)

    captures = list(
        (
            await db_session.scalars(
                select(RawJobRecord).where(
                    RawJobRecord.job_id.in_([job.id for job in jobs])
                )
            )
        ).all()
    )
    assert captures
    assert all(capture.source == "scalejobs" for capture in captures)
    # The untouched publisher payload, kept for a later scoring version.
    assert all("sourceType" in capture.raw_payload for capture in captures)


async def test_descriptions_are_summaries_not_employer_prose(
    db_session: AsyncSession,
) -> None:
    """Stated plainly: this portal's text is short, and that is what it is."""
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("scalejobs", build_scalejobs())]
    )

    described = [
        job for job in await jobs_of(db_session, prepared.run.id) if job.description
    ]
    assert described
    for job in described:
        assert len(job.description or "") >= 200
        assert "<p>" not in (job.description or "")


async def test_a_listing_without_real_text_is_not_padded_to_qualify(
    db_session: AsyncSession,
) -> None:
    """One captured listing carries too little text to be scoreable.

    It must be rejected as unreadable rather than composed up to the floor.
    """
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("scalejobs", build_scalejobs())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    undescribed = [job for job in jobs if not job.description]
    assert len(undescribed) == 1

    matches = await matches_of(db_session, prepared.run.id)
    rejected = [
        match
        for match in matches
        if match.job_id == undescribed[0].id and not match.qualified
    ]
    assert rejected
    reasons = " ".join(
        str(reason) for reason in (rejected[0].rejection_reasons or [])
    ).lower()
    assert "description" in reasons


async def test_every_returned_scalejobs_job_links_to_the_employers_ats(
    db_session: AsyncSession,
) -> None:
    """Scale.jobs publishes no per-job page, so the link is the real one."""
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("scalejobs", build_scalejobs())]
    )

    assert outcome.results
    for item in outcome.results:
        assert item.preferred_application_url.startswith("http")
        assert "scale.jobs" not in item.preferred_application_url
        assert "linkedin.com" not in item.preferred_application_url


async def test_the_ats_link_is_not_an_aggregator_hop(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("scalejobs", build_scalejobs())]
    )

    jobs = await jobs_of(db_session, prepared.run.id)
    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id.in_([j.id for j in jobs]))
            )
        ).all()
    )
    assert sources
    # These point where the application is submitted, not at another board.
    assert not any(
        source.url_kind is JobUrlKind.AGGREGATOR_REDIRECT for source in sources
    )


async def test_the_freshness_ladder_widens_when_the_newest_window_is_thin(
    db_session: AsyncSession,
) -> None:
    """This portal filters by date itself, so each window is a real request.

    The fixture spans months, so the 24 hour window alone cannot fill a quota
    and the ladder has to widen to collect the whole of it.
    """
    _, resume = await analysed_resume(db_session)
    records = scalejobs_records()
    recent = [
        record
        for record in records
        if str(record.get("datePosted") or "")[:10] >= "2026-08-03"
    ]
    assert 0 < len(recent) < len(records)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("scalejobs", build_scalejobs())], limit=100
    )

    assert prepared.legs[0].raw_count == len(records)


# --- Scale.jobs beside each existing portal --------------------------------


async def test_jobright_plus_scalejobs_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("jobright", build_jobright()), ("scalejobs", build_scalejobs())],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 2
    assert all(leg.raw_count > 0 for leg in prepared.legs)
    assert any(carried_by(item, "scalejobs") for item in outcome.results)


async def test_hiring_cafe_plus_scalejobs_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("hiringcafe", build_hiringcafe()), ("scalejobs", build_scalejobs())],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert any(carried_by(item, "hiringcafe") for item in outcome.results)
    assert any(carried_by(item, "scalejobs") for item in outcome.results)


async def test_simplyhired_plus_scalejobs_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("simplyhired", build_simplyhired()), ("scalejobs", build_scalejobs())],
        limit=50,
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert any(carried_by(item, "simplyhired") for item in outcome.results)
    assert any(carried_by(item, "scalejobs") for item in outcome.results)


async def test_greenhouse_plus_scalejobs_merges_both_portals(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "greenhouse",
                build_greenhouse(qualifying_greenhouse(company="Northwind Trading")),
            ),
            ("scalejobs", build_scalejobs()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert any(carried_by(item, "greenhouse") for item in outcome.results)
    assert any(carried_by(item, "scalejobs") for item in outcome.results)


async def test_existing_portals_are_unaffected_by_scalejobs_being_selected(
    db_session: AsyncSession,
) -> None:
    """Adding a portal must not change what another one returns."""
    _, resume = await analysed_resume(db_session)

    alone, _ = await run_search(
        db_session, resume=resume, specs=[("hc-alone", build_hiringcafe())]
    )
    together, _ = await run_search(
        db_session,
        resume=resume,
        specs=[("hc-with", build_hiringcafe()), ("sj-with", build_scalejobs())],
        limit=50,
    )

    assert alone.legs[0].raw_count == together.legs[0].raw_count
    assert alone.legs[0].qualified_count == together.legs[0].qualified_count


async def test_all_five_operational_portals_run_in_one_search(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
            (
                "greenhouse",
                build_greenhouse(qualifying_greenhouse(company="Northwind Trading")),
            ),
            ("scalejobs", build_scalejobs()),
        ],
        limit=50,
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 5
    assert all(leg.raw_count > 0 for leg in prepared.legs)
    assert all(leg.status is PortalRunStatus.COMPLETED for leg in prepared.legs)


async def test_quotas_are_shared_across_five_portals(
    db_session: AsyncSession,
) -> None:
    """CLAUDE.md section 5: an approximately equal share per selected portal."""
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
            (
                "greenhouse",
                build_greenhouse(qualifying_greenhouse(company="Northwind Trading")),
            ),
            ("scalejobs", build_scalejobs()),
        ],
        limit=20,
    )

    assert [leg.target_quota for leg in prepared.legs] == [20] * 5
    assert sum(leg.selected_count for leg in prepared.legs) <= 20


async def test_the_five_portal_result_is_ranked_and_free_of_duplicates(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
            (
                "greenhouse",
                build_greenhouse(qualifying_greenhouse(company="Northwind Trading")),
            ),
            ("scalejobs", build_scalejobs()),
        ],
        limit=50,
    )

    assert_ranked_newest_first(outcome.results)
    # Across five portals the pool is wide enough to contain the case the
    # ranking contract exists for, so assert it is actually present rather
    # than merely permitted.
    assert_recency_outranks_score(outcome.results)

    job_ids = [item.job_id for item in outcome.results]
    assert len(job_ids) == len(set(job_ids))
    assert all(item.final_score >= 70.0 for item in outcome.results)


# --- Cross-portal deduplication involving Scale.jobs -----------------------


def _mirror_as_scalejobs(record: dict, *, job_id: str = "ext:mirror___000001") -> dict:
    """Recast one Hiring Cafe listing as the same vacancy on Scale.jobs."""
    processed = record["v5_processed_job_data"]
    summary = processed.get("requirements_summary") or ""
    activities = processed.get("role_activities") or []
    tools = processed.get("technical_tools") or []

    parts = [summary]
    if tools:
        parts.append("Technical Tools:\n" + ", ".join(tools))
    if activities:
        bullets = "\n".join(f"- {item}" for item in activities[:12])
        parts.append("Responsibilities:\n" + bullets)

    return {
        "id": job_id,
        "externalId": job_id,
        "title": record["job_information"]["title"],
        "organization": record["enriched_company_data"]["name"],
        "url": "https://jobs.workable.com/view/mirror000001/financial-analyst",
        "descriptionText": "\n\n".join(part for part in parts if part),
        "datePosted": "2026-07-30T20:41:50.852Z",
        "employmentType": "Full Time",
        "aiEmploymentType": "Full Time",
        "aiWorkArrangement": "Hybrid",
        "aiExperienceLevel": "Mid Level",
        "sourceType": "ats",
        "source": "workable",
        "jobLocations": [{"city": processed.get("formatted_workplace_location")}],
    }


async def test_one_vacancy_on_hiring_cafe_and_scalejobs_is_one_job(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe([shared])),
            ("scalejobs", build_scalejobs([_mirror_as_scalejobs(shared)])),
        ],
    )

    company = shared["enriched_company_data"]["name"]
    jobs = [
        job
        for job in (await db_session.scalars(select(Job))).all()
        if job.company == company
    ]
    assert len(jobs) == 1, [job.title for job in jobs]

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == jobs[0].id)
            )
        ).all()
    )
    assert len(sources) == 2

    item = next(item for item in outcome.results if item.job_id == jobs[0].id)
    assert carried_by(item, "hiringcafe")
    assert carried_by(item, "scalejobs")


async def test_one_vacancy_on_all_five_portals_is_still_one_job(
    db_session: AsyncSession,
) -> None:
    """The requirement stated for Phase 9E, exercised directly.

    One canonical job, five sources, provenance preserved for each.
    """
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright([_mirror_as_jobright(shared)])),
            ("hiringcafe", build_hiringcafe([shared])),
            ("simplyhired", build_simplyhired([_mirror_as_simplyhired(shared)])),
            ("greenhouse", build_greenhouse([_mirror_as_greenhouse(shared)])),
            ("scalejobs", build_scalejobs([_mirror_as_scalejobs(shared)])),
        ],
    )

    company = shared["enriched_company_data"]["name"]
    jobs = [
        job
        for job in (await db_session.scalars(select(Job))).all()
        if job.company == company
    ]
    assert len(jobs) == 1, [job.title for job in jobs]

    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == jobs[0].id)
            )
        ).all()
    )
    assert len(sources) == 5

    item = next(item for item in outcome.results if item.job_id == jobs[0].id)
    assert len(item.portal_slugs) == 5
    for slug in ("jobright", "hiringcafe", "simplyhired", "greenhouse", "scalejobs"):
        assert carried_by(item, slug)


async def test_a_duplicate_across_five_portals_takes_one_quota_slot(
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
            ("simplyhired", build_simplyhired([_mirror_as_simplyhired(shared)])),
            ("greenhouse", build_greenhouse([_mirror_as_greenhouse(shared)])),
            ("scalejobs", build_scalejobs([_mirror_as_scalejobs(shared)])),
        ],
    )

    # One vacancy, however many portals carried it.
    assert len(outcome.results) == 1


async def test_the_preferred_url_rules_still_decide_with_scalejobs_present(
    db_session: AsyncSession,
) -> None:
    """Both links are direct; the existing preference order still chooses."""
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe([shared])),
            ("scalejobs", build_scalejobs([_mirror_as_scalejobs(shared)])),
        ],
    )

    assert len(outcome.results) == 1
    chosen = outcome.results[0].preferred_application_url
    assert chosen.startswith("http")
    # Never a scale.jobs page - the portal publishes none.
    assert "scale.jobs" not in chosen


async def test_scoring_does_not_depend_on_scalejobs_finding_the_job(
    db_session: AsyncSession,
) -> None:
    shared = hiringcafe_records()[0]
    _, resume = await analysed_resume(db_session)

    _, alone = await run_search(
        db_session, resume=resume, specs=[("hc-only", build_hiringcafe([shared]))]
    )
    _, both = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hc-both", build_hiringcafe([shared])),
            ("sj-both", build_scalejobs([_mirror_as_scalejobs(shared)])),
        ],
    )

    assert alone.results and both.results
    assert alone.results[0].final_score == both.results[0].final_score


async def test_rediscovering_a_scalejobs_listing_creates_no_second_job(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    records = [_mirror_as_scalejobs(hiringcafe_records()[0])]

    first, _ = await run_search(
        db_session, resume=resume, specs=[("sj-first", build_scalejobs(records))]
    )
    before = list((await db_session.scalars(select(Job))).all())

    second, _ = await run_search(
        db_session, resume=resume, specs=[("sj-second", build_scalejobs(records))]
    )
    after = list((await db_session.scalars(select(Job))).all())

    assert len(after) == len(before)
    assert first.legs[0].raw_count == second.legs[0].raw_count


async def test_repeating_a_scalejobs_search_keeps_its_scores(
    db_session: AsyncSession,
) -> None:
    """Scores and depth repeat exactly; the row count can move by one, and why.

    Phase 9E recorded that Scale.jobs republishes one Deaconess requisition
    under two employer names - "Deaconess" and "Deaconess Health System" -
    and that canonicalisation will not declare two differently-named employers
    the same one on the strength of a shared title. Which variant claims the
    plain canonical key depends on what is already stored **and on the order
    the listings are processed in**.

    Phase 12 changed that order: the portal now walks the whole freshness
    ladder instead of stopping at the first window, so its listings arrive
    spread across windows rather than in one batch. That is enough to surface
    the second Deaconess variant on a repeat run.

    What must not move is the ranking, and it does not: every job present in
    both runs keeps its exact score, and the collection depth is identical.
    """
    _, resume = await analysed_resume(db_session)

    first_run, first = await run_search(
        db_session, resume=resume, specs=[("sj-a", build_scalejobs())], limit=50
    )
    await db_session.commit()

    second_run, second = await run_search(
        db_session, resume=resume, specs=[("sj-b", build_scalejobs())], limit=50
    )

    assert first_run.legs[0].raw_count == second_run.legs[0].raw_count

    before = {item.job_id: item.final_score for item in first.results}
    shared = [item for item in second.results if item.job_id in before]
    assert shared, "the second run rediscovered nothing at all"
    for item in shared:
        assert item.final_score == before[item.job_id]

    for outcome in (first, second):
        assert_ranked_newest_first(outcome.results)
        assert all(item.final_score >= 70.0 for item in outcome.results)


async def test_repeating_a_five_portal_search_keeps_ranking_stable(
    db_session: AsyncSession,
) -> None:
    """Scores and order are stable; the *set* is not asserted, and here is why.

    Scale.jobs republishes one requisition under more than one employer name -
    the captured fixture carries the same Deaconess vacancy as "Deaconess" and
    as "Deaconess Health System", which is also the name Hiring Cafe uses.
    Canonicalisation keys on the normalised company, and it will not declare
    two differently-named employers the same one on the strength of a shared
    title: that is the conservative choice, and it is the right one.

    The consequence is that when two portals carry that vacancy under two
    names, which variant claims the plain canonical key depends on what is
    already stored, so the exact set can differ by one row between runs. What
    must not move is the ranking: every job present in both runs keeps its
    score, and both runs are ordered by it. That is what this asserts.

    Scale.jobs alone is exactly stable - see the test above. This is a
    pre-existing property of Phase 5 canonicalisation meeting real duplicate
    data, not something the adapter does.
    """
    _, resume = await analysed_resume(db_session)

    def specs(tag: str):
        return [
            (f"jr-{tag}", build_jobright()),
            (f"hc-{tag}", build_hiringcafe()),
            (f"sh-{tag}", build_simplyhired()),
            (
                f"gh-{tag}",
                build_greenhouse(qualifying_greenhouse(company="Northwind Trading")),
            ),
            (f"sj-{tag}", build_scalejobs()),
        ]

    _, first = await run_search(db_session, resume=resume, specs=specs("one"), limit=50)
    await db_session.commit()

    _, second = await run_search(db_session, resume=resume, specs=specs("two"), limit=50)

    for outcome in (first, second):
        assert_ranked_newest_first(outcome.results)
        ids = [item.job_id for item in outcome.results]
        assert len(ids) == len(set(ids))
        assert all(item.final_score >= 70.0 for item in outcome.results)

    before = {item.job_id: item.final_score for item in first.results}
    shared = [item for item in second.results if item.job_id in before]
    assert shared, "the second run rediscovered nothing at all"
    for item in shared:
        assert item.final_score == before[item.job_id]


async def test_one_employer_under_two_names_stays_two_jobs(
    db_session: AsyncSession,
) -> None:
    """Documents the conservative rule above, with the real captured data.

    Scale.jobs publishes this vacancy as "Deaconess" and Hiring Cafe as
    "Deaconess Health System". Merging them would need evidence the two names
    are one employer, which nothing here has, so both stand - each with its own
    sources and its own score.
    """
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe()),
            ("scalejobs", build_scalejobs()),
        ],
        limit=50,
    )

    deaconess = [
        item for item in outcome.results if "deaconess" in item.company.lower()
    ]
    assert len(deaconess) > 1
    assert len({item.company for item in deaconess}) > 1
    # Still one row per canonical job, and still ranked.
    assert len({item.job_id for item in deaconess}) == len(deaconess)


# --- Failure isolation, in every direction ---------------------------------


async def test_scalejobs_failing_still_returns_the_other_portals_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("hiringcafe", build_hiringcafe()),
            (
                "scalejobs",
                build_scalejobs(
                    failure=PortalTimeoutError("scalejobs down", portal="scalejobs")
                ),
            ),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.results
    assert all(carried_by(item, "hiringcafe") for item in outcome.results)

    failed = [leg for leg in prepared.legs if leg.status is PortalRunStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].error


async def test_another_portal_failing_does_not_stop_scalejobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "hiringcafe",
                build_hiringcafe(
                    failure=PortalParseError("hc down", portal="hiringcafe")
                ),
            ),
            ("scalejobs", build_scalejobs()),
        ],
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.results
    assert all(carried_by(item, "scalejobs") for item in outcome.results)


async def test_four_of_five_failing_still_completes_with_errors(
    db_session: AsyncSession,
) -> None:
    """One surviving portal must still deliver a usable search."""
    _, resume = await analysed_resume(db_session)

    down = PortalTimeoutError("down", portal="x")
    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright(failure=down)),
            ("hiringcafe", build_hiringcafe(failure=down)),
            ("simplyhired", build_simplyhired(failure=down)),
            ("greenhouse", build_greenhouse(failure=down)),
            ("scalejobs", build_scalejobs()),
        ],
        limit=50,
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.results
    assert all(carried_by(item, "scalejobs") for item in outcome.results)

    failed = [leg for leg in prepared.legs if leg.status is PortalRunStatus.FAILED]
    assert len(failed) == 4


async def test_all_five_failing_fails_the_run_with_every_error_recorded(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    down = PortalTimeoutError("down", portal="x")
    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright(failure=down)),
            ("hiringcafe", build_hiringcafe(failure=down)),
            ("simplyhired", build_simplyhired(failure=down)),
            ("greenhouse", build_greenhouse(failure=down)),
            ("scalejobs", build_scalejobs(failure=down)),
        ],
    )

    assert outcome.status is SearchRunStatus.FAILED
    assert outcome.results == []
    assert all(leg.status is PortalRunStatus.FAILED for leg in prepared.legs)
    assert all(leg.error for leg in prepared.legs)


# --- Eligibility is unchanged ----------------------------------------------


async def test_an_applied_job_stays_excluded_when_scalejobs_finds_it(
    db_session: AsyncSession,
) -> None:
    """CLAUDE.md section 9: rediscovery must not resurrect an applied job."""
    shared = hiringcafe_records()[0]
    user, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("hc-first", build_hiringcafe([shared]))]
    )
    assert outcome.results
    job_id = outcome.results[0].job_id

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

    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[("sj-later", build_scalejobs([_mirror_as_scalejobs(shared)]))],
    )

    assert job_id not in {item.job_id for item in second.results}


# --- US-only location filter (Phase 10) -------------------------------------
#
# The captured Greenhouse fixture is the useful one here: it holds twelve real
# postings, five of which are outside the United States - Bengaluru, London,
# Japan and two Canada-only remote roles. They were accepted before this phase
# and are rejected now, which is what these tests pin down.
#
# The filter runs during normalisation, so a non-US listing is never scored,
# never deduplicated and never occupies a portal's quota slot.

#: Of the twelve captured postings, five are outside the United States.
NON_US_IN_FIXTURE = 5
US_IN_FIXTURE = 7

#: Of those twelve, the adapter's title gate collects three for this resume's
#: queries - "Financial Analyst, Business F&S" in Bengaluru, and the Retailer
#: role posted twice, once US-remote and once Canada-only. Two of the three are
#: therefore outside the US, which is what the pipeline tests below count.
NON_US_COLLECTED = 2
US_COLLECTED = 1


def _greenhouse_locations() -> list[str]:
    return [
        (job.get("location") or {}).get("name") for job in greenhouse_records()
    ]


def test_the_greenhouse_fixture_really_does_carry_non_us_postings() -> None:
    """Guards the premise of the tests below."""
    from app.processing.us_locations import classify_us_location

    verdicts = [classify_us_location(name) for name in _greenhouse_locations()]

    assert sum(1 for v in verdicts if not v.is_us) == NON_US_IN_FIXTURE
    assert sum(1 for v in verdicts if v.is_us) == US_IN_FIXTURE


async def test_non_us_listings_are_dropped_before_they_are_scored(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    # Everything the portal returned is still counted as collected: the filter
    # is not hidden by pretending the listings were never seen.
    assert prepared.legs[0].raw_count == US_COLLECTED + NON_US_COLLECTED

    jobs = await jobs_of(db_session, prepared.run.id)
    assert len(jobs) == US_COLLECTED

    from app.processing.us_locations import classify_us_location

    for job in jobs:
        assert classify_us_location(job.location).is_us, job.location


async def test_a_dropped_listing_is_recorded_with_its_reason(
    db_session: AsyncSession,
) -> None:
    """The audit trail has to say why, not merely that the job vanished."""
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    captures = list(
        (
            await db_session.scalars(
                select(RawJobRecord).where(
                    RawJobRecord.search_run_portal_id == prepared.legs[0].id
                )
            )
        ).all()
    )
    non_us = [
        capture
        for capture in captures
        if capture.rejection_reason == RejectionReason.NON_US_LOCATION
    ]

    assert len(non_us) == NON_US_COLLECTED
    assert all(capture.job_id is None for capture in non_us)


async def test_a_non_us_listing_never_takes_a_quota_slot(
    db_session: AsyncSession,
) -> None:
    """A filtered job must not crowd out a US one."""
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("greenhouse", build_greenhouse())],
        limit=20,
    )

    from app.processing.us_locations import classify_us_location

    for item in outcome.results:
        assert classify_us_location(item.location).is_us, item.location


@pytest.mark.parametrize(
    "location",
    ["London, United Kingdom", "Toronto, Canada", "Sydney, Australia",
     "Bengaluru, India", "Berlin, Germany", "Remote", "Worldwide", None],
)
async def test_a_previously_acceptable_listing_is_now_filtered(
    db_session: AsyncSession, location: str | None
) -> None:
    """The regression this phase exists to create.

    Each of these would have become a job before Phase 10. The vacancy is
    otherwise identical to one that qualifies, so only the location decides.
    """
    _, resume = await analysed_resume(db_session)

    record = _mirror_as_greenhouse(hiringcafe_records()[0])
    record["location"] = {"name": location}

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse([record]))]
    )

    assert prepared.legs[0].raw_count == 1
    assert await jobs_of(db_session, prepared.run.id) == []
    assert outcome.results == []


@pytest.mark.parametrize(
    "location",
    ["Austin, TX", "Boston, Massachusetts", "Seattle", "Remote - United States",
     "US Remote", "Chicago, IL, USA"],
)
async def test_the_same_listing_in_the_us_still_qualifies(
    db_session: AsyncSession, location: str
) -> None:
    """The filter must not be quietly rejecting everything."""
    _, resume = await analysed_resume(db_session)

    record = _mirror_as_greenhouse(hiringcafe_records()[0])
    record["location"] = {"name": location}

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse([record]))]
    )

    assert outcome.results, location
    assert all(item.final_score >= 70.0 for item in outcome.results)


# --- Every portal, individually and together --------------------------------


@pytest.mark.parametrize(
    ("slug", "builder", "recast"),
    [
        ("jobright", build_jobright, "_mirror_as_jobright"),
        ("hiringcafe", build_hiringcafe, None),
        ("simplyhired", build_simplyhired, "_mirror_as_simplyhired"),
        ("greenhouse", build_greenhouse, "_mirror_as_greenhouse"),
        ("scalejobs", build_scalejobs, "_mirror_as_scalejobs"),
    ],
)
async def test_each_portal_filters_its_own_non_us_listings(
    db_session: AsyncSession, slug: str, builder, recast: str | None
) -> None:
    """The filter is portal-agnostic: it runs once, for everyone."""
    _, resume = await analysed_resume(db_session)

    shared = hiringcafe_records()[0]
    if recast is None:
        record = json.loads(json.dumps(shared))
        record["v5_processed_job_data"]["formatted_workplace_location"] = (
            "London, United Kingdom"
        )
    else:
        record = globals()[recast](shared)
        if slug == "jobright":
            record["jobResult"]["jobLocation"] = "London, United Kingdom"
        elif slug == "simplyhired":
            record["location"] = "London, United Kingdom"
        elif slug == "greenhouse":
            record["location"] = {"name": "London, United Kingdom"}
        elif slug == "scalejobs":
            record["jobLocations"] = [{"city": "London", "country": "GB"}]

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[(slug, builder([record]))]
    )

    assert prepared.legs[0].raw_count == 1
    assert outcome.results == []
    assert await jobs_of(db_session, prepared.run.id) == []


async def test_a_combined_search_returns_only_us_jobs(
    db_session: AsyncSession,
) -> None:
    """End to end, every operational portal, real fixtures."""
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright()),
            ("hiringcafe", build_hiringcafe()),
            ("simplyhired", build_simplyhired()),
            ("greenhouse", build_greenhouse()),
            ("scalejobs", build_scalejobs()),
        ],
        limit=50,
    )

    from app.processing.us_locations import classify_us_location

    assert outcome.results
    for item in outcome.results:
        verdict = classify_us_location(item.location)
        assert verdict.is_us, f"{item.company}: {item.location!r}"


# --- What the run tells the user --------------------------------------------


async def test_the_run_summary_records_the_us_only_policy(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, _ = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    summary = prepared.run.summary
    assert summary["location_policy"] == "united_states_only"
    assert summary["non_us_excluded_count"] == NON_US_COLLECTED
    assert summary["portals"][0]["non_us_excluded"] == NON_US_COLLECTED


async def test_the_run_says_it_considered_only_us_jobs(
    db_session: AsyncSession,
) -> None:
    """Stated on every run, whether or not anything was dropped."""
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    notes = " ".join(outcome.warnings)
    assert "Only United States jobs were considered." in notes
    assert f"{NON_US_COLLECTED} listings outside the US were excluded" in notes


async def test_the_policy_is_stated_even_when_nothing_was_excluded(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("simplyhired", build_simplyhired())]
    )

    notes = " ".join(outcome.warnings)
    assert "Only United States jobs were considered." in notes
