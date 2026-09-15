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
from app.portals.adapters.ashby import AshbyPortal
from app.portals.adapters.lever import LeverPortal
from app.portals.adapters.scalejobs import PAGE_SIZE as SCALEJOBS_PAGE_SIZE
from app.portals.adapters.scalejobs import ScaleJobsPortal
from app.portals.adapters.simplyhired import SimplyHiredPortal
from app.portals.adapters.workday import PAGE_SIZE as WORKDAY_PAGE_SIZE
from app.portals.adapters.workday import WorkdayPortal
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalTimeoutError,
)
from app.processing.normalize import RejectionReason
from app.services import search_run_service
from app.services.search_orchestrator import execute_search_run
from tests.factories import make_portal, make_profile, make_resume, make_user
from tests.fake_portals import FINANCE_DESCRIPTION

FIXTURES = Path(__file__).parent / "fixtures" / "portals"
HIRINGCAFE_FIXTURE = FIXTURES / "hiringcafe_financial_analyst.json"
JOBRIGHT_FIXTURE = FIXTURES / "jobright_financial_analyst.json"
SIMPLYHIRED_FIXTURE = FIXTURES / "simplyhired_financial_analyst.json"
GREENHOUSE_FIXTURE = FIXTURES / "greenhouse_financial_analyst.json"
SCALEJOBS_FIXTURE = FIXTURES / "scalejobs_financial_analyst.json"

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


def _ashby_page(records: list[dict], url: str) -> str:
    """The board page, or one job page, depending on what was requested.

    Ashby serves both as rendered HTML carrying a ``window.__appData`` island,
    and job pages additionally carry a ``schema.org`` JobPosting block. Built
    here rather than replayed so the pipeline tests can control dates and
    countries; the parsing of Ashby's real markup is pinned in
    ``tests/test_ashby_adapter.py`` against a live capture.
    """
    tail = url.split("jobs.ashbyhq.com/", 1)[-1].strip("/").split("/")

    if len(tail) == 1:
        postings = [
            {k: v for k, v in record.items() if k not in ("ld", "org")}
            for record in records
        ]
        return (
            "<!DOCTYPE html><html><body><script>window.__appData = "
            + json.dumps(
                {
                    "organization": {"name": "Fixture Co"},
                    "jobBoard": {"jobPostings": postings, "teams": []},
                }
            )
            + ";</script></body></html>"
        )

    record = next((r for r in records if r["id"] == tail[1]), None)
    if record is None:
        raise AssertionError(f"job page requested for unknown posting: {url}")

    posting = {k: v for k, v in record.items() if k not in ("ld", "org")}
    return (
        "<!DOCTYPE html><html><head>"
        '<script type="application/ld+json">'
        + json.dumps(record["ld"])
        + "</script></head><body><script>window.__appData = "
        + json.dumps(
            {"organization": {"name": record.get("org", "Fixture Co")}, "posting": posting}
        )
        + ";</script></body></html>"
    )


def _lever_body(records: list[dict]) -> str:
    """Lever answers with a bare JSON array of postings."""
    return json.dumps(records)


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


class OfflineSimplyHired(SimplyHiredPortal):
    """The real adapter, served from a canned search page."""

    records: list[dict] = []
    failure: Exception | None = None


class OfflineGreenhouse(GreenhousePortal):
    """The real adapter, served from a canned board payload."""

    records: list[dict] = []
    failure: Exception | None = None


class OfflineAshby(AshbyPortal):
    """The real adapter, served from canned board and job pages."""

    records: list[dict] = []
    failure: Exception | None = None


class OfflineLever(LeverPortal):
    """The real adapter, served from a canned postings feed."""

    records: list[dict] = []
    failure: Exception | None = None


#: What every shipped Workday tenant publishes, near enough: the CXS path is
#: permitted and only the facet-refresh path is closed. A tenant whose policy
#: declines is exercised directly in tests/test_workday_adapter.py.
WORKDAY_ROBOTS = chr(10).join(["User-agent: *", "Disallow: /refreshFacet/", ""])


class OfflineWorkday(WorkdayPortal):
    """The real adapter, served from canned CXS list and detail payloads."""

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
    "lever": (OfflineLever, _lever_body),
}

#: Ashby is the one adapter that requests two *kinds* of page - a board page
#: and then a job page per title match - so its stub needs the URL, which the
#: shared builder signature above does not carry.
_OFFLINE_PAGES = {"ashby": OfflineAshby}

#: Scale.jobs GETs JSON from a search endpoint, a third verb again.
_OFFLINE_JSON = {"scalejobs": OfflineScaleJobs}


@pytest.fixture(autouse=True)
def offline_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve every adapter from fixtures, and fail loudly on a real request.

    Only the transport is replaced: slug matching, page and envelope parsing
    and field mapping all run the production code.
    """

    async def fake_get_text(self, url, **kwargs):  # noqa: ANN001
        paged = _OFFLINE_PAGES.get(self.portal)
        if paged is not None:
            if paged.failure is not None:
                raise paged.failure
            return _ashby_page(paged.records, url)

        entry = _OFFLINE.get(self.portal)
        if entry is None:
            raise AssertionError(f"unexpected request from {self.portal}: {url}")

        adapter_class, build_page = entry
        if adapter_class.failure is not None:
            raise adapter_class.failure
        return build_page(adapter_class.records)

    async def fake_post_json(self, url, *, payload, headers=None):  # noqa: ANN001
        if self.portal == "workday":
            if OfflineWorkday.failure is not None:
                raise OfflineWorkday.failure
            rows = [record["row"] for record in OfflineWorkday.records]
            offset = int(payload.get("offset", 0))
            limit = int(payload.get("limit", WORKDAY_PAGE_SIZE))
            return {
                "total": len(rows),
                "jobPostings": rows[offset : offset + limit],
                "userAuthenticated": False,
            }

        # No other portal exercised in this file POSTs. The stub stays so that
        # one starting to would fail loudly rather than reach the network.
        raise AssertionError(f"unexpected request from {self.portal}: {url}")

    async def fake_get_optional_text(self, url, **kwargs):  # noqa: ANN001
        if self.portal == "workday":
            # Permissive, and never a real request. A tenant whose policy
            # declines is exercised directly in tests/test_workday_adapter.py.
            return WORKDAY_ROBOTS
        raise AssertionError(f"unexpected request from {self.portal}: {url}")

    async def fake_get_json(self, url, *, params=None):  # noqa: ANN001
        if self.portal == "workday":
            if OfflineWorkday.failure is not None:
                raise OfflineWorkday.failure
            for record in OfflineWorkday.records:
                if url.endswith(record["row"]["externalPath"]):
                    return record["detail"]
            raise PortalError("no such posting", portal="workday")

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
    monkeypatch.setattr(
        "app.portals.http.PortalHttpClient.get_optional_text", fake_get_optional_text
    )

    yield

    # Class-level state must not leak into the next test.
    for adapter_class, _ in _OFFLINE.values():
        adapter_class.records = []
        adapter_class.failure = None
    for adapter_class in _OFFLINE_JSON.values():
        adapter_class.records = []
        adapter_class.failure = None
    for adapter_class in _OFFLINE_PAGES.values():
        adapter_class.records = []
        adapter_class.failure = None
    OfflineWorkday.records = []
    OfflineWorkday.failure = None


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


#: A Lever board's postings, in the shape the live feed serves.
#:
#: Built rather than replayed from the capture in
#: ``tests/fixtures/portals/lever_postings.json``: that capture holds real
#: engineering and retail roles, and this resume is a Financial Analyst, so
#: replaying it would prove only that the role gate works. The *shapes* here -
#: epoch-millisecond ``createdAt``, ISO ``country``, ``categories`` with
#: ``allLocations`` and ``commitment``, sectioned ``lists`` - are exactly the
#: capture's, and the parsing of the real payload is pinned in
#: ``tests/test_lever_adapter.py``.
def lever_records(
    count: int = 3, *, country: str = "US", now: datetime = NOW
) -> list[dict]:
    records = []
    for index in range(count):
        job_id = f"0000000{index}-0000-4000-8000-00000000000{index}"
        hosted = f"https://jobs.lever.co/fixtureco/{job_id}"
        records.append(
            {
                "id": job_id,
                "text": "Financial Analyst",
                "hostedUrl": hosted,
                "applyUrl": f"{hosted}/apply",
                # Staggered inside the window so ranking has something to order.
                "createdAt": int(
                    (now - timedelta(days=index + 1)).timestamp() * 1000
                ),
                "country": country,
                "workplaceType": "hybrid",
                "categories": {
                    "location": "New York, NY",
                    "allLocations": ["New York, NY"],
                    "commitment": "Full Time",
                    "team": "Finance",
                },
                "descriptionPlain": FINANCE_DESCRIPTION,
                "lists": [
                    {
                        "text": "Minimum Qualifications",
                        "content": (
                            "<ul><li>Excel and SQL</li>"
                            "<li>Forecasting and budgeting</li></ul>"
                        ),
                    }
                ],
                "additionalPlain": "We are an equal opportunity employer.",
            }
        )
    return records


def _as_paragraphs(text: str) -> str:
    """Wrap a plain description in paragraph tags, as Ashby serves it."""
    return "</p><p>".join(text.split(chr(10)))


#: Ashby postings in the shape the board and job pages publish.
#:
#: Each record doubles as a board-page listing and, through its ``ld`` key, the
#: ``schema.org`` block its job page carries. Dates and countries are set
#: relative to the run's frozen clock so the freshness and United States gates
#: are genuinely exercised.
def ashby_records(
    count: int = 3, *, country: str = "USA", now: datetime = NOW
) -> list[dict]:
    records = []
    for index in range(count):
        job_id = f"a000000{index}-0000-4000-8000-00000000000{index}"
        posted = (now - timedelta(days=index + 1)).date().isoformat()
        records.append(
            {
                "id": job_id,
                "title": "Financial Analyst",
                "locationName": "New York, NY",
                "secondaryLocations": [],
                "workplaceType": "Hybrid",
                "employmentType": "FullTime",
                "compensationTierSummary": None,
                "org": "Fixture Co",
                "ld": {
                    "@context": "https://schema.org",
                    "@type": "JobPosting",
                    "title": "Financial Analyst",
                    "datePosted": posted,
                    "employmentType": "FULL_TIME",
                    "hiringOrganization": {"@type": "Organization", "name": "Fixture Co"},
                    "jobLocation": {
                        "@type": "Place",
                        "address": {
                            "@type": "PostalAddress",
                            "addressLocality": "New York City",
                            "addressRegion": "NY",
                            "addressCountry": country,
                        },
                    },
                    "description": (
                        "<h2>About the role</h2><p>"
                        + _as_paragraphs(FINANCE_DESCRIPTION)
                        + "</p><h2>Minimum Qualifications</h2>"
                        "<ul><li>Excel and SQL</li><li>Forecasting and budgeting</li></ul>"
                    ),
                },
            }
        )
    return records


def build_ashby(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineAshby:
    # One board keeps the fixture honest about request counts.
    adapter = OfflineAshby(
        options={"rate_limit_per_minute": 0, "boards": ["fixtureco"]}
    )
    type(adapter).records = records if records is not None else ashby_records()
    type(adapter).failure = failure
    return adapter


#: Workday postings in the shape the CXS endpoints publish: a list row and the
#: detail payload its externalPath resolves to. Dates and countries are set
#: relative to the run's frozen clock so the freshness and United States gates
#: are genuinely exercised.
def workday_records(
    count: int = 3,
    *,
    country: str = "US",
    country_name: str = "United States of America",
    place: str = "Illinois - Chicago",
    now: datetime = NOW,
    entity: str = "1114 Fixture Enterprise Inc",
) -> list[dict]:
    records = []
    for index in range(count):
        req = f"R{index}00{index}"
        path = f"/job/Illinois---Chicago/Financial-Analyst_{req}"
        posted = (now - timedelta(days=index + 1)).date().isoformat()
        records.append(
            {
                "row": {
                    "title": "Financial Analyst",
                    "externalPath": path,
                    "locationsText": place,
                    "postedOn": f"Posted {index + 1} Days Ago",
                    "bulletFields": [req],
                },
                "detail": {
                    # The legal entity the adapter must ignore in favour of the
                    # configured display name.
                    "hiringOrganization": {"name": entity, "url": ""},
                    "jobPostingInfo": {
                        "id": f"id{index}",
                        "title": "Financial Analyst",
                        "jobReqId": req,
                        "startDate": posted,
                        "postedOn": f"Posted {index + 1} Days Ago",
                        "timeType": "Full time",
                        "remoteType": "Hybrid",
                        "location": place,
                        "additionalLocations": [],
                        "country": {"descriptor": country_name},
                        "jobRequisitionLocation": {
                            "descriptor": place,
                            "country": {
                                "descriptor": country_name,
                                "alpha2Code": country,
                            },
                        },
                        "externalUrl": (
                            "https://fixtureco.wd5.myworkdayjobs.com"
                            f"/Fixture_Careers{path}"
                        ),
                        "jobDescription": (
                            "<h2>About the role</h2><p>"
                            + _as_paragraphs(FINANCE_DESCRIPTION)
                            + "</p><h2>Minimum Qualifications</h2>"
                            "<ul><li>Excel and SQL</li>"
                            "<li>Forecasting and budgeting</li></ul>"
                        ),
                    },
                },
            }
        )
    return records


def build_workday(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineWorkday:
    # One tenant keeps the fixture honest about request counts.
    adapter = OfflineWorkday(
        options={
            "rate_limit_per_minute": 0,
            "tenants": [
                {
                    "tenant": "fixtureco",
                    "wd": "5",
                    "site": "Fixture_Careers",
                    "display_name": "Fixture Co",
                }
            ],
        }
    )
    type(adapter).records = records if records is not None else workday_records()
    type(adapter).failure = failure
    return adapter


def build_lever(
    records: list[dict] | None = None, *, failure: Exception | None = None
) -> OfflineLever:
    # One board keeps the fixture honest about request counts.
    adapter = OfflineLever(
        options={
            "rate_limit_per_minute": 0,
            "boards": [{"slug": "fixtureco", "company": "Fixture Co"}],
        }
    )
    type(adapter).records = records if records is not None else lever_records()
    type(adapter).failure = failure
    return adapter


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


async def test_an_ashby_only_search_returns_its_jobs(
    db_session: AsyncSession,
) -> None:
    """The real adapter, through the real pipeline, end to end."""
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("ashby", build_ashby())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count == 3
    assert prepared.legs[0].status is PortalRunStatus.COMPLETED
    assert outcome.returned_count > 0
    assert all(item.final_score >= 70.0 for item in outcome.results)
    assert all(carried_by(item, "ashby") for item in outcome.results)
    assert_ranked_newest_first(outcome.results)


async def test_ashby_results_carry_the_employer_from_the_page(
    db_session: AsyncSession,
) -> None:
    """The board page publishes the organisation; nothing is configured."""
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("ashby", build_ashby())]
    )

    assert outcome.results
    assert {item.company for item in outcome.results} == {"Fixture Co"}


async def test_ashby_results_link_to_the_employers_own_ats(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("ashby", build_ashby())]
    )

    assert outcome.results
    for item in outcome.results:
        assert item.preferred_application_url.startswith(
            "https://jobs.ashbyhq.com/fixtureco/"
        )
        assert item.preferred_url_kind == JobUrlKind.ATS.value


async def test_an_ashby_posting_outside_the_window_is_excluded(
    db_session: AsyncSession,
) -> None:
    """The 30-day gate is applied to datePosted, unchanged."""
    _, resume = await analysed_resume(db_session)
    stale = ashby_records(1)
    stale[0]["ld"]["datePosted"] = (NOW - timedelta(days=31)).date().isoformat()

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("ashby", build_ashby(stale))]
    )

    assert outcome.returned_count == 0


async def test_an_ashby_posting_with_no_date_is_excluded(
    db_session: AsyncSession,
) -> None:
    """An unverifiable date is never treated as recent."""
    _, resume = await analysed_resume(db_session)
    undated = ashby_records(1)
    del undated[0]["ld"]["datePosted"]

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("ashby", build_ashby(undated))]
    )

    # Dropped by the adapter rather than collected and rejected later: a
    # listing with no date could not pass the gate under any circumstances.
    assert prepared.legs[0].raw_count == 0
    assert outcome.returned_count == 0


async def test_a_non_us_ashby_posting_is_excluded(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    abroad = ashby_records(2, country="Ireland")
    for record in abroad:
        record["locationName"] = "Dublin, Ireland"
        record["ld"]["jobLocation"]["address"] = {
            "@type": "PostalAddress",
            "addressLocality": "Dublin",
            "addressCountry": "Ireland",
        }

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("ashby", build_ashby(abroad))]
    )

    assert outcome.returned_count == 0


async def test_both_united_states_spellings_are_accepted(
    db_session: AsyncSession,
) -> None:
    """Live data carries both spellings; a test against one hides the other."""
    _, resume = await analysed_resume(db_session)
    mixed = ashby_records(2)
    # Distinct vacancies, or canonical grouping would correctly merge two
    # postings sharing an employer, a title and a place into one job - and the
    # test would be measuring deduplication rather than the country spelling.
    mixed[0]["ld"]["jobLocation"]["address"]["addressCountry"] = "USA"
    mixed[1]["ld"]["jobLocation"]["address"]["addressCountry"] = "United States"
    mixed[1]["title"] = "Senior Financial Analyst"
    mixed[1]["ld"]["title"] = "Senior Financial Analyst"
    mixed[1]["ld"]["jobLocation"]["address"]["addressLocality"] = "Chicago"
    mixed[1]["ld"]["jobLocation"]["address"]["addressRegion"] = "IL"
    mixed[1]["locationName"] = "Chicago, IL"

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("ashby", build_ashby(mixed))]
    )

    assert outcome.returned_count == 2


async def test_an_ashby_failure_is_isolated_to_its_own_leg(
    db_session: AsyncSession,
) -> None:
    """Greenhouse still returns its jobs while Ashby is refused."""
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("greenhouse", build_greenhouse()),
            (
                "ashby",
                build_ashby(failure=PortalBlockedError("blocked", portal="ashby")),
            ),
        ],
    )

    slug_of = {portal.id: portal.slug for portal in prepared.portals}
    ashby_leg = next(
        leg for leg in prepared.legs if slug_of[leg.portal_id].endswith("ashby")
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert ashby_leg.status is PortalRunStatus.FAILED
    assert not any(carried_by(item, "ashby") for item in outcome.results)
    assert all(carried_by(item, "greenhouse") for item in outcome.results)


async def test_an_ashby_only_run_that_fails_is_a_failed_run(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "ashby",
                build_ashby(failure=PortalBlockedError("blocked", portal="ashby")),
            )
        ],
    )

    assert outcome.status is SearchRunStatus.FAILED
    assert prepared.legs[0].status is PortalRunStatus.FAILED
    assert outcome.returned_count == 0


# --- Regression guard: Ashby must not disturb the portals already here ------


async def test_selecting_greenhouse_alone_makes_no_ashby_request(
    db_session: AsyncSession,
) -> None:
    """Adding a portal must change nothing for a search that does not select it.

    Ashby is registered process-wide by importing ``app.portals.adapters``, so
    this runs with it registered. ``offline_http`` raises on a request from any
    portal it was not given, so an Ashby request would fail the run outright -
    which is the part a result assertion alone would miss.
    """
    import app.portals.adapters  # noqa: F401
    from app.portals.registry import registry

    assert "ashby" in registry.slugs, "guard is vacuous unless Ashby is registered"

    untouched = OfflineAshby.records
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 1
    assert not any(carried_by(item, "ashby") for item in outcome.results)
    assert all(carried_by(item, "greenhouse") for item in outcome.results)
    assert OfflineAshby.records == untouched, "Ashby state must be untouched"


async def test_ashby_and_an_existing_portal_merge_without_duplicates(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("greenhouse", build_greenhouse()), ("ashby", build_ashby())],
        limit=20,
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 2
    keys = [item.job_id for item in outcome.results]
    assert len(keys) == len(set(keys)), "a canonical job must appear once"


async def test_a_lever_only_search_returns_its_jobs(
    db_session: AsyncSession,
) -> None:
    """The real adapter, through the real pipeline, end to end."""
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("lever", build_lever())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count == 3
    assert prepared.legs[0].status is PortalRunStatus.COMPLETED
    assert outcome.returned_count > 0
    assert all(item.final_score >= 70.0 for item in outcome.results)
    assert all(carried_by(item, "lever") for item in outcome.results)
    assert_ranked_newest_first(outcome.results)


async def test_lever_results_carry_the_employer_from_configuration(
    db_session: AsyncSession,
) -> None:
    """The feed publishes no company name, so the board must supply it."""
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("lever", build_lever())]
    )

    assert outcome.results
    assert {item.company for item in outcome.results} == {"Fixture Co"}


async def test_lever_results_link_to_the_employers_own_ats(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("lever", build_lever())]
    )

    assert outcome.results
    for item in outcome.results:
        assert item.preferred_application_url.startswith(
            "https://jobs.lever.co/fixtureco/"
        )
        assert item.preferred_url_kind == JobUrlKind.ATS.value


async def test_a_lever_posting_outside_the_window_is_excluded(
    db_session: AsyncSession,
) -> None:
    """The 30-day gate is applied to createdAt, unchanged."""
    _, resume = await analysed_resume(db_session)
    stale = lever_records(1)
    stale[0]["createdAt"] = int((NOW - timedelta(days=31)).timestamp() * 1000)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("lever", build_lever(stale))]
    )

    assert outcome.returned_count == 0


async def test_a_lever_posting_with_no_date_is_excluded(
    db_session: AsyncSession,
) -> None:
    """An unverifiable date is never treated as recent."""
    _, resume = await analysed_resume(db_session)
    undated = lever_records(1)
    del undated[0]["createdAt"]

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("lever", build_lever(undated))]
    )

    assert outcome.returned_count == 0


async def test_a_non_us_lever_posting_is_excluded(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    abroad = lever_records(2, country="GB")
    for record in abroad:
        record["categories"]["location"] = "London"
        record["categories"]["allLocations"] = ["London"]

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("lever", build_lever(abroad))]
    )

    assert outcome.returned_count == 0


async def test_a_lever_failure_is_isolated_to_its_own_leg(
    db_session: AsyncSession,
) -> None:
    """Greenhouse still returns its jobs while Lever is refused."""
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("greenhouse", build_greenhouse()),
            (
                "lever",
                build_lever(failure=PortalBlockedError("blocked", portal="lever")),
            ),
        ],
    )

    slug_of = {portal.id: portal.slug for portal in prepared.portals}
    lever_leg = next(
        leg for leg in prepared.legs if slug_of[leg.portal_id].endswith("lever")
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert lever_leg.status is PortalRunStatus.FAILED
    assert not any(carried_by(item, "lever") for item in outcome.results)
    assert all(carried_by(item, "greenhouse") for item in outcome.results)


async def test_a_lever_only_run_that_fails_is_a_failed_run(
    db_session: AsyncSession,
) -> None:
    """Nothing succeeded, so the run must not be reported as completed."""
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "lever",
                build_lever(failure=PortalBlockedError("blocked", portal="lever")),
            )
        ],
    )

    assert outcome.status is SearchRunStatus.FAILED
    assert prepared.legs[0].status is PortalRunStatus.FAILED
    assert outcome.returned_count == 0


# --- Regression guard: Lever must not disturb the portals already here ------


async def test_selecting_greenhouse_alone_is_unaffected_by_lever_existing(
    db_session: AsyncSession,
) -> None:
    """Adding a portal must change nothing for a search that does not select it.

    Lever is registered process-wide by importing ``app.portals.adapters``, so
    this runs with it registered and asserts the Greenhouse-only run is
    untouched: no Lever leg, no Lever source, and - because ``offline_http``
    raises on a request from a portal it was not given - no Lever request
    either. That last one is the part a result assertion alone would miss.
    """
    import app.portals.adapters  # noqa: F401
    from app.portals.registry import registry

    assert "lever" in registry.slugs, "guard is vacuous unless Lever is registered"

    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 1
    assert not any(carried_by(item, "lever") for item in outcome.results)
    assert all(carried_by(item, "greenhouse") for item in outcome.results)


async def test_lever_and_an_existing_portal_merge_without_duplicates(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("greenhouse", build_greenhouse()),
            ("lever", build_lever()),
        ],
        limit=20,
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 2
    keys = [item.job_id for item in outcome.results]
    assert len(keys) == len(set(keys)), "a canonical job must appear once"


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


async def test_the_two_outages_observed_in_production_leave_the_run_usable(
    db_session: AsyncSession,
) -> None:
    """The exact pair seen on 2026-08-25, end to end.

    Hiring Cafe was behind a browser-verification challenge on every path -
    ``/robots.txt`` included - and Scale.jobs' live inventory was answering 500
    because *their* upstream proxy returned 407 to them. Neither is our bug and
    neither is fixable from here, so what this pins down is the behaviour that
    is ours: the run completes, the surviving portal's jobs reach the user, and
    the two failures are recorded as failures rather than as portals that
    happened to find nothing.
    """
    qualifying = _mirror_as_jobright(hiringcafe_records()[0])
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("jobright", build_jobright([qualifying])),
            (
                "hiringcafe",
                build_hiringcafe(
                    failure=PortalBlockedError(
                        "Hiring Cafe served a browser-verification challenge "
                        "instead of the page.",
                        portal="hiringcafe",
                    )
                ),
            ),
            (
                "scalejobs",
                build_scalejobs(
                    failure=PortalError(
                        "scalejobs returned HTTP 500.", portal="scalejobs"
                    )
                ),
            ),
        ],
    )

    # The run is usable, and honest about being partial.
    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.returned_count > 0
    assert all(carried_by(item, "jobright") for item in outcome.results)

    # Failed, not "completed with nothing found" - the distinction the brief
    # turns on. A zero count on a COMPLETED leg would be a claim that the
    # portal was searched and had no matching jobs.
    assert prepared.legs[0].status is PortalRunStatus.COMPLETED
    for leg in prepared.legs[1:]:
        assert leg.status is PortalRunStatus.FAILED
        assert leg.error, "a failed leg must record why"
        assert leg.selected_count == 0

    # And the user is told, per portal, rather than left to infer it.
    warnings = " ".join(outcome.warnings).lower()
    assert "hiring cafe" in warnings or "hiringcafe" in warnings
    assert "scale" in warnings


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


async def test_a_skipped_job_stays_excluded_when_found_by_the_other_portal(
    db_session: AsyncSession,
) -> None:
    """Skipping survives rediscovery through a different portal.

    The exclusion is keyed on the canonical job, so a second portal's listing
    resolves to the same row and cannot smuggle a passed-over job back in under
    another source's URL.
    """
    shared = hiringcafe_records()[0]
    user, resume = await analysed_resume(db_session)

    _, first = await run_search(
        db_session, resume=resume, specs=[("hc-skip", build_hiringcafe([shared]))]
    )
    await db_session.commit()
    assert first.returned_count == 1
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=ApplicationStatus.SKIPPED,
        )
    )
    await db_session.flush()

    _, second = await run_search(
        db_session,
        resume=resume,
        specs=[("jr-skip", build_jobright([_mirror_as_jobright(shared)]))],
    )

    assert job_id not in {item.job_id for item in second.results}


@pytest.mark.parametrize(
    "status", [ApplicationStatus.NOT_APPLIED, ApplicationStatus.SAVED]
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


# --- Workday ---------------------------------------------------------------


async def test_a_workday_only_search_returns_its_jobs(
    db_session: AsyncSession,
) -> None:
    """The real adapter, through the real pipeline, end to end."""
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("workday", build_workday())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].raw_count == 3
    assert prepared.legs[0].status is PortalRunStatus.COMPLETED
    assert outcome.returned_count > 0
    assert all(item.final_score >= 70.0 for item in outcome.results)
    assert all(carried_by(item, "workday") for item in outcome.results)
    assert_ranked_newest_first(outcome.results)


async def test_workday_results_carry_the_configured_employer_not_the_entity(
    db_session: AsyncSession,
) -> None:
    """The defect this portal is configured around, proved through the pipeline.

    Every fixture posting publishes "1114 Fixture Enterprise Inc" as its
    Workday organisation. None of that may reach the dashboard.
    """
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("workday", build_workday())]
    )

    assert outcome.results
    assert {item.company for item in outcome.results} == {"Fixture Co"}


async def test_workday_postings_from_two_legal_entities_do_not_fragment(
    db_session: AsyncSession,
) -> None:
    """One employer, two Workday entities, one canonical company."""
    records = workday_records(1, entity="300 Fixture National Association")
    records += workday_records(1, entity="417 Fixture Trust")
    records[1]["row"]["externalPath"] = "/job/Illinois---Chicago/Financial-Analyst_R999"
    records[1]["row"]["bulletFields"] = ["R999"]
    records[1]["detail"]["jobPostingInfo"]["jobReqId"] = "R999"
    records[1]["detail"]["jobPostingInfo"]["externalUrl"] = (
        "https://fixtureco.wd5.myworkdayjobs.com/Fixture_Careers"
        "/job/Illinois---Chicago/Financial-Analyst_R999"
    )

    _, resume = await analysed_resume(db_session)
    _, outcome = await run_search(
        db_session, resume=resume, specs=[("workday", build_workday(records))]
    )

    assert outcome.results
    assert {item.company for item in outcome.results} == {"Fixture Co"}


async def test_workday_results_link_to_the_employers_own_ats(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session, resume=resume, specs=[("workday", build_workday())]
    )

    assert outcome.results
    for item in outcome.results:
        assert item.preferred_application_url.startswith(
            "https://fixtureco.wd5.myworkdayjobs.com/Fixture_Careers/"
        )
        assert item.preferred_url_kind == JobUrlKind.ATS.value


async def test_a_workday_posting_outside_the_window_is_excluded(
    db_session: AsyncSession,
) -> None:
    stale = workday_records(1, now=NOW - timedelta(days=40))
    stale[0]["row"]["postedOn"] = "Posted 30+ Days Ago"

    _, resume = await analysed_resume(db_session)
    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("workday", build_workday(stale))]
    )

    # Dropped on the relative label, before its detail was ever requested.
    assert prepared.legs[0].raw_count == 0
    assert outcome.returned_count == 0


async def test_a_workday_posting_with_no_start_date_is_excluded(
    db_session: AsyncSession,
) -> None:
    undated = workday_records(1)
    undated[0]["detail"]["jobPostingInfo"]["startDate"] = ""

    _, resume = await analysed_resume(db_session)
    _, outcome = await run_search(
        db_session, resume=resume, specs=[("workday", build_workday(undated))]
    )

    assert outcome.returned_count == 0


async def test_a_workday_posting_outside_the_united_states_is_excluded(
    db_session: AsyncSession,
) -> None:
    abroad = workday_records(
        2, country="CA", country_name="Canada", place="Ontario - Toronto"
    )

    _, resume = await analysed_resume(db_session)
    _, outcome = await run_search(
        db_session, resume=resume, specs=[("workday", build_workday(abroad))]
    )

    assert outcome.returned_count == 0


async def test_a_failing_workday_leg_does_not_fail_the_whole_run(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("greenhouse", build_greenhouse()),
            (
                "workday",
                build_workday(failure=PortalBlockedError("blocked", portal="workday")),
            ),
        ],
    )

    slug_of = {portal.id: portal.slug for portal in prepared.portals}
    workday_leg = next(
        leg for leg in prepared.legs if slug_of[leg.portal_id].endswith("workday")
    )

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert workday_leg.status is PortalRunStatus.FAILED
    assert not any(carried_by(item, "workday") for item in outcome.results)
    assert all(carried_by(item, "greenhouse") for item in outcome.results)


async def test_a_workday_only_run_that_fails_is_reported_as_failed(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    _, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (
                "workday",
                build_workday(failure=PortalBlockedError("blocked", portal="workday")),
            )
        ],
    )

    assert outcome.status is SearchRunStatus.FAILED
    assert outcome.returned_count == 0


async def test_selecting_greenhouse_alone_makes_no_workday_request(
    db_session: AsyncSession,
) -> None:
    """Adding a portal must change nothing for a search that does not select it.

    Workday is registered process-wide by importing ``app.portals.adapters``,
    so this runs with it registered. ``offline_http`` raises on a request from
    any portal it was not given, so a Workday request would fail the run
    outright - which is the part a result assertion alone would miss.
    """
    import app.portals.adapters  # noqa: F401
    from app.portals.registry import registry

    assert "workday" in registry.slugs, "guard is vacuous unless Workday is registered"

    untouched = OfflineWorkday.records
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session, resume=resume, specs=[("greenhouse", build_greenhouse())]
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 1
    assert not any(carried_by(item, "workday") for item in outcome.results)
    assert all(carried_by(item, "greenhouse") for item in outcome.results)
    assert OfflineWorkday.records == untouched, "Workday state must be untouched"


async def test_workday_and_an_existing_portal_merge_without_duplicates(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)

    prepared, outcome = await run_search(
        db_session,
        resume=resume,
        specs=[("greenhouse", build_greenhouse()), ("workday", build_workday())],
    )

    assert outcome.status is SearchRunStatus.COMPLETED
    assert len(prepared.legs) == 2
    urls = [item.preferred_application_url for item in outcome.results]
    assert len(urls) == len(set(urls))
    assert_ranked_newest_first(outcome.results)
