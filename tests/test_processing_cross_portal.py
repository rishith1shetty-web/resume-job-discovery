"""Processing listings from two portals at once, and the preview endpoint.

**These are fixture-based tests, not live verification.** The Jobright records
come from a capture taken in Phase 4. No portal is contacted by any test in
this file, and nothing here demonstrates that two portals were collected from
live: the two-portal cases below are constructed records, deliberately, since a
genuine simultaneous capture is impossible to commit.

What they do show is that the processing layer is portal-agnostic: it merges,
separates and ranks URLs the same way whichever adapter produced the records.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import JobUrlKind
from app.models.job import Job, JobSource
from app.models.search import SearchRun
from app.portals.base import RawJob
from app.services.job_processing import process_raw_jobs
from tests.factories import make_portal

FIXTURES = Path(__file__).parent / "fixtures" / "portals"
NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
BASE = "/api/v1/portals"


# --- Real captured data -----------------------------------------------------


@pytest.fixture
def jobright_records() -> list[RawJob]:
    """Real Jobright listings, parsed by the Phase 4 adapter from a capture."""
    from app.portals.adapters.jobright import JobrightPortal

    payload = json.loads(
        (FIXTURES / "jobright_financial_analyst.json").read_text(encoding="utf-8")
    )
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps({'props': payload['props']})}</script>"
    )

    adapter = JobrightPortal()
    records = [
        raw
        for record in adapter._extract_records(html)
        if (raw := adapter._to_raw_job(record)) is not None
    ]
    assert records, "the Jobright fixture produced no records"
    return records


async def test_real_jobright_listings_all_normalise(
    db_session: AsyncSession, jobright_records: list[RawJob]
) -> None:
    portal = await make_portal(db_session, slug="jr", is_implemented=True)

    result = await process_raw_jobs(
        db_session, portal=portal, raw_jobs=jobright_records, now=NOW
    )

    assert result.rejected == [], "real listings should survive normalisation"
    assert len(result.processed) == len(jobright_records)

    jobs = (
        await db_session.scalars(
            select(Job).options(selectinload(Job.sources))
        )
    ).all()
    for job in jobs:
        # Every job must give the user something to open and apply through.
        assert job.preferred_application_url is not None
        assert job.preferred_application_url.startswith("https://")
        assert job.title
        assert job.sources


async def test_jobrights_own_job_page_is_kept_as_the_usable_link(
    db_session: AsyncSession, jobright_records: list[RawJob]
) -> None:
    """Jobright exposes no employer-direct URL, and none is invented.

    Its records carry only a jobright.ai job page, so that is what is stored
    and what the user opens. Claiming it were employer-direct would rank an
    unverified link above one that works.
    """
    portal = await make_portal(db_session, slug="jr", is_implemented=True)

    result = await process_raw_jobs(
        db_session, portal=portal, raw_jobs=jobright_records[:3], now=NOW
    )

    jobs = (await db_session.scalars(select(Job))).all()
    assert jobs
    for job in jobs:
        assert job.preferred_url_kind is JobUrlKind.PORTAL_LISTING
        assert "jobright.ai" in (job.preferred_application_url or "")
    assert result.jobs_created == len(result.processed)


# --- Two portals over one vacancy ------------------------------------------


def cross_portal_pair(
    *, ats_url: str | None = None
) -> tuple[RawJob, RawJob]:
    """The same vacancy as two portals would describe it.

    Constructed, not collected: capturing the same vacancy on two portals at
    the same moment is not something that can be committed as a fixture.

    The second record deliberately carries an aggregator URL, because that is
    what the URL-preference rule has to rank *below* an employer or ATS link.
    The host is one ``app.processing.urls`` still classifies as an aggregator -
    remaining portals surface such links - so this stays a real input even
    though no adapter collects from that site.
    """
    first = RawJob(
        external_id="6a71f26571acd469eeda4ea3",
        source="jobright-like",
        title="Senior Financial Analyst- Remote",
        company="Contoso",
        location="Boston, MA",
        description="Own the monthly close and build the operating model. " * 12,
        source_url="https://jobright.ai/jobs/info/6a71f26571acd469eeda4ea3?utm_source=1014",
        application_url=ats_url
        or "https://jobright.ai/jobs/info/6a71f26571acd469eeda4ea3?utm_source=1014",
        posted_at=NOW - timedelta(days=2),
        employment_type="Full-time",
        raw_payload={},
    )
    second = RawJob(
        external_id="3901234567",
        source="aggregator-like",
        title="Sr. Financial Analyst (Remote)",
        company="Contoso Ltd",
        location="Boston",
        description="Own the monthly close.",
        source_url="https://www.linkedin.com/jobs/view/3901234567?refId=abc",
        application_url=ats_url or "https://www.linkedin.com/jobs/view/3901234567",
        posted_at=NOW - timedelta(days=1),
        employment_type=None,
        raw_payload={},
    )
    return first, second


async def test_one_vacancy_on_two_portals_becomes_one_job_with_two_sources(
    db_session: AsyncSession
) -> None:
    alpha = await make_portal(db_session, slug="a", is_implemented=True)
    beta = await make_portal(db_session, slug="b", is_implemented=True)
    first, second = cross_portal_pair()

    a = await process_raw_jobs(db_session, portal=alpha, raw_jobs=[first], now=NOW)
    b = await process_raw_jobs(db_session, portal=beta, raw_jobs=[second], now=NOW)

    assert a.processed[0].job_id == b.processed[0].job_id
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert await db_session.scalar(select(func.count()).select_from(JobSource)) == 2


async def test_the_better_value_from_either_portal_wins(
    db_session: AsyncSession
) -> None:
    alpha = await make_portal(db_session, slug="a", is_implemented=True)
    beta = await make_portal(db_session, slug="b", is_implemented=True)
    first, second = cross_portal_pair()

    # Deliberately the weaker record first, so the merge has to improve on it.
    await process_raw_jobs(db_session, portal=beta, raw_jobs=[second], now=NOW)
    result = await process_raw_jobs(db_session, portal=alpha, raw_jobs=[first], now=NOW)

    job = await db_session.scalar(
        select(Job).where(Job.id == result.processed[0].job_id)
    )
    assert job is not None
    assert job.location == "Boston, MA", "the more specific location should win"
    assert len(job.description or "") > 200, "the fuller description should win"
    assert job.employment_type == "Full-time"
    assert job.posted_at == NOW - timedelta(days=2), "the earliest date should win"


async def test_a_shared_ats_link_is_preferred_over_both_portal_pages(
    db_session: AsyncSession
) -> None:
    ats = "https://boards.greenhouse.io/contoso/jobs/4123456?gh_jid=4123456"
    alpha = await make_portal(db_session, slug="a", is_implemented=True)
    beta = await make_portal(db_session, slug="b", is_implemented=True)
    first, second = cross_portal_pair(ats_url=ats)

    await process_raw_jobs(db_session, portal=alpha, raw_jobs=[first], now=NOW)
    result = await process_raw_jobs(db_session, portal=beta, raw_jobs=[second], now=NOW)

    job = await db_session.scalar(
        select(Job)
        .where(Job.id == result.processed[0].job_id)
        .options(selectinload(Job.sources))
    )
    assert job is not None
    assert job.preferred_url_kind is JobUrlKind.ATS
    assert job.preferred_application_url == ats
    # Both portals' own listing URLs survive on their source rows.
    assert {source.source_url for source in job.sources} == {
        "https://jobright.ai/jobs/info/6a71f26571acd469eeda4ea3",
        "https://www.linkedin.com/jobs/view/3901234567",
    }


async def test_processing_two_portals_twice_is_idempotent(
    db_session: AsyncSession
) -> None:
    alpha = await make_portal(db_session, slug="a", is_implemented=True)
    beta = await make_portal(db_session, slug="b", is_implemented=True)
    first, second = cross_portal_pair()

    for _ in range(2):
        await process_raw_jobs(db_session, portal=alpha, raw_jobs=[first], now=NOW)
        await process_raw_jobs(db_session, portal=beta, raw_jobs=[second], now=NOW)

    assert await db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert await db_session.scalar(select(func.count()).select_from(JobSource)) == 2


# --- The preview endpoint ---------------------------------------------------


@pytest.fixture
def stub_jobright(monkeypatch: pytest.MonkeyPatch):
    """Answer from the committed capture, never the network."""
    from app.portals.adapters.jobright import JobrightPortal

    payload = json.loads(
        (FIXTURES / "jobright_financial_analyst.json").read_text(encoding="utf-8")
    )
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps({'props': payload['props']})}</script>"
    )

    monkeypatch.setattr(
        JobrightPortal, "_taxonomy_cache", frozenset({"financial-analyst"})
    )

    async def _get_text(self, url, params=None):  # noqa: ANN001
        assert "/api/" not in url, "the disallowed API surface must not be used"
        return html

    async def _head_ok(self, url):  # noqa: ANN001
        return True

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _get_text)
    monkeypatch.setattr("app.portals.http.PortalHttpClient.head_ok", _head_ok)


async def test_the_preview_endpoint_reports_processing_decisions(
    api_client: AsyncClient, db_session: AsyncSession, stub_jobright
) -> None:
    from app.services.portal_seed import ensure_portals_seeded

    await ensure_portals_seeded(db_session)

    response = await api_client.post(
        f"{BASE}/jobright/processing-preview",
        json={"queries": ["Financial Analyst"], "limit": 3},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True
    assert data["collected"] > 0
    assert data["canonical_groups"] > 0

    job = data["jobs"][0]
    assert job["preferred_application_url"].startswith("https://")
    assert job["preferred_url_kind"] == "portal_listing"
    assert job["canonical_key"]
    assert job["group_index"] == 0


async def test_the_preview_endpoint_stores_nothing(
    api_client: AsyncClient, db_session: AsyncSession, stub_jobright
) -> None:
    from app.services.portal_seed import ensure_portals_seeded

    await ensure_portals_seeded(db_session)

    await api_client.post(
        f"{BASE}/jobright/processing-preview",
        json={"queries": ["Financial Analyst"], "limit": 3},
    )

    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0
    assert await db_session.scalar(select(func.count()).select_from(JobSource)) == 0
    assert await db_session.scalar(select(func.count()).select_from(SearchRun)) == 0


async def test_the_preview_endpoint_reports_a_blocked_portal_as_data(
    api_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal is a diagnostic, not a 500.

    The two portals that refused before any request were removed in D-005, so
    the refusal is induced here. What is under test is the endpoint's handling,
    not any one portal's access state.
    """
    from app.portals.exceptions import PortalBlockedError
    from app.services.portal_seed import ensure_portals_seeded

    await ensure_portals_seeded(db_session)

    async def _blocked(self, url, params=None):  # noqa: ANN001
        raise PortalBlockedError("refused access", portal="jobright")

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _blocked)

    response = await api_client.post(
        f"{BASE}/jobright/processing-preview",
        json={"queries": ["Financial Analyst"], "limit": 3},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is False
    assert data["error_code"] == "portal_blocked"
    assert data["jobs"] == []


async def test_the_preview_endpoint_404s_for_an_unknown_portal(
    api_client: AsyncClient
) -> None:
    response = await api_client.post(
        f"{BASE}/not-a-portal/processing-preview", json={"queries": ["Analyst"]}
    )

    assert response.status_code == 404
