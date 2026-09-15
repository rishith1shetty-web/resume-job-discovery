"""Portal seeding and the read-only probe endpoint.

Integration tests run inside a transaction that is always rolled back, so
nothing is committed to resume_job_discovery. No test here reaches a real
portal: every adapter is driven through a stubbed transport.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import app.portals.adapters  # noqa: F401  - registers the adapters
from app.models.portal import Portal
from app.models.search import SearchRun
from app.portals.exceptions import PortalBlockedError, PortalTimeoutError
from app.portals.registry import registry
from app.services.portal_seed import (
    PORTAL_SEEDS,
    PortalSeed,
    ensure_portals_seeded,
)

BASE = "/api/v1/portals"


@pytest.fixture
def jobright_html() -> str:
    """Wrap the captured Jobright records the way the live page embeds them."""
    import json

    payload = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "portals"
            / "jobright_financial_analyst.json"
        ).read_text(encoding="utf-8")
    )
    inner = json.dumps({"props": payload["props"]})
    return (
        '<script id="__NEXT_DATA__" type="application/json">' f"{inner}</script>"
    )


@pytest.fixture
def stub_jobright(monkeypatch: pytest.MonkeyPatch, jobright_html: str):
    """Make the Jobright adapter answer from the fixture, never the network."""
    from app.portals.adapters.jobright import JobrightPortal

    monkeypatch.setattr(
        JobrightPortal, "_taxonomy_cache", frozenset({"financial-analyst"})
    )

    async def _get_text(self, url, params=None):  # noqa: ANN001
        assert "/api/" not in url, "the disallowed API surface must not be used"
        return jobright_html

    async def _head_ok(self, url):  # noqa: ANN001
        return True

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _get_text)
    monkeypatch.setattr("app.portals.http.PortalHttpClient.head_ok", _head_ok)


# --- Seeding ----------------------------------------------------------------


async def test_seeding_creates_the_operational_portals(db_session: AsyncSession) -> None:
    await ensure_portals_seeded(db_session)

    slugs = set(
        (await db_session.execute(select(Portal.slug))).scalars().all()
    )
    assert {"jobright", "greenhouse", "google_careers"} <= slugs


async def test_workday_is_seeded_with_its_own_depth(
    db_session: AsyncSession,
) -> None:
    """Workday needs more time per call than the shared default allows.

    One ``search`` fans out across 29 employer hosts, so a cold pass overruns
    the 90s per-call ceiling and starves the freshness ladder of the shared leg
    budget. These are set for this portal alone, through the row the depth is
    read from.
    """
    from app.models.portal import PortalConfig
    from app.search.depth import SearchDepth
    from app.services.search_orchestrator import adapter_options

    await ensure_portals_seeded(db_session)

    portal = await db_session.scalar(
        select(Portal)
        .where(Portal.slug == "workday")
        .options(selectinload(Portal.config))
    )
    assert portal is not None
    assert portal.config is not None

    depth = SearchDepth.from_options(adapter_options(portal))

    assert depth.query_timeout_seconds == 150.0
    assert depth.max_runtime_seconds == 300.0


#: Portals whose shape needs depth of their own, and exactly what they get.
#: Everything absent from this table must resolve to the shared defaults, which
#: is what stops a per-portal escape hatch turning into a global one.
EXPECTED_DEPTH_OVERRIDES = {
    # One call fans out across 29 employer hosts.
    "workday": {"query_timeout_seconds": 150.0, "max_runtime_seconds": 300.0},
    # Each call fits the shared ceiling; the whole leg does not, because every
    # board shares one hostname and is therefore paced end to end.
    "smartrecruiters": {"max_runtime_seconds": 300.0},
}


async def test_only_the_portals_that_need_their_own_depth_have_it(
    db_session: AsyncSession,
) -> None:
    """The change must be invisible to every portal that did not ask for it."""
    from app.search.depth import (
        DEFAULT_MAX_RUNTIME_SECONDS,
        DEFAULT_QUERY_TIMEOUT_SECONDS,
        SearchDepth,
    )
    from app.services.search_orchestrator import adapter_options

    await ensure_portals_seeded(db_session)

    portals = (
        (
            await db_session.execute(
                select(Portal).options(selectinload(Portal.config))
            )
        )
        .scalars()
        .all()
    )

    for portal in portals:
        expected = EXPECTED_DEPTH_OVERRIDES.get(portal.slug, {})
        depth = SearchDepth.from_options(adapter_options(portal))

        assert depth.query_timeout_seconds == expected.get(
            "query_timeout_seconds", DEFAULT_QUERY_TIMEOUT_SECONDS
        ), portal.slug
        assert depth.max_runtime_seconds == expected.get(
            "max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS
        ), portal.slug


async def test_smartrecruiters_keeps_the_shared_query_ceiling(
    db_session: AsyncSession,
) -> None:
    """Its calls already fit 90s; only the leg needed more room."""
    from app.search.depth import DEFAULT_QUERY_TIMEOUT_SECONDS, SearchDepth
    from app.services.search_orchestrator import adapter_options

    await ensure_portals_seeded(db_session)

    portal = await db_session.scalar(
        select(Portal)
        .where(Portal.slug == "smartrecruiters")
        .options(selectinload(Portal.config))
    )
    assert portal is not None
    depth = SearchDepth.from_options(adapter_options(portal))

    assert depth.query_timeout_seconds == DEFAULT_QUERY_TIMEOUT_SECONDS == 90.0
    assert depth.max_runtime_seconds == 300.0


async def test_seeding_never_overwrites_an_operators_own_tuning(
    db_session: AsyncSession,
) -> None:
    """A redeploy must not reset a value someone deliberately changed."""
    from app.search.depth import SearchDepth
    from app.services.search_orchestrator import adapter_options

    await ensure_portals_seeded(db_session)

    portal = await db_session.scalar(
        select(Portal)
        .where(Portal.slug == "workday")
        .options(selectinload(Portal.config))
    )
    assert portal is not None and portal.config is not None
    portal.config.options = {**portal.config.options, "query_timeout_seconds": 240.0}
    await db_session.flush()

    await ensure_portals_seeded(db_session)
    await db_session.refresh(portal.config)

    depth = SearchDepth.from_options(adapter_options(portal))

    assert depth.query_timeout_seconds == 240.0
    # The key the operator did not touch is still supplied.
    assert depth.max_runtime_seconds == 300.0


async def test_seeding_supplies_a_missing_option_without_touching_the_others(
    db_session: AsyncSession,
) -> None:
    from app.services.search_orchestrator import adapter_options

    await ensure_portals_seeded(db_session)

    portal = await db_session.scalar(
        select(Portal)
        .where(Portal.slug == "workday")
        .options(selectinload(Portal.config))
    )
    assert portal is not None and portal.config is not None
    portal.config.options = {"tenants": [], "max_concurrent_tenants": 4}
    await db_session.flush()

    await ensure_portals_seeded(db_session)
    await db_session.refresh(portal.config)

    options = adapter_options(portal)

    assert options["max_concurrent_tenants"] == 4
    assert options["query_timeout_seconds"] == 150.0
    assert options["max_runtime_seconds"] == 300.0


async def test_seeding_is_idempotent(db_session: AsyncSession) -> None:
    """Create-then-update, using a throwaway slug.

    Deliberately not the real slugs: those rows are legitimate configuration
    that may already be committed, and a test must not depend on whether an
    operator has seeded them yet.
    """
    from tests.conftest import TEST_RECORD_MARKER

    seeds = (
        PortalSeed(
            slug=f"{TEST_RECORD_MARKER}-portal",
            website_url="https://example.invalid/",
            display_order=999,
            is_enabled=True,
            notes=TEST_RECORD_MARKER,
        ),
    )

    first = await ensure_portals_seeded(db_session, seeds=seeds)
    second = await ensure_portals_seeded(db_session, seeds=seeds)

    assert set(first.values()) == {"created"}
    assert set(second.values()) == {"updated"}

    count = await db_session.scalar(
        select(func.count())
        .select_from(Portal)
        .where(Portal.slug == seeds[0].slug)
    )
    assert count == 1


async def test_capability_flags_come_from_the_adapter(
    db_session: AsyncSession,
) -> None:
    # The database must not disagree with what the adapter actually does.
    await ensure_portals_seeded(db_session)

    for slug in ("jobright", "greenhouse"):
        portal = await db_session.scalar(select(Portal).where(Portal.slug == slug))
        capabilities = registry.create(slug, {}).capabilities

        assert portal is not None
        assert portal.is_implemented is True
        assert portal.supports_date_filter == capabilities.supports_date_filter
        assert portal.requires_auth == capabilities.requires_auth


async def test_seeding_does_not_re_enable_a_portal_an_operator_disabled(
    db_session: AsyncSession,
) -> None:
    # An operator's decision to switch a portal off must survive a redeploy.
    from tests.conftest import TEST_RECORD_MARKER

    seeds = (
        PortalSeed(
            slug=f"{TEST_RECORD_MARKER}-disabled",
            website_url="https://example.invalid/",
            display_order=998,
            is_enabled=True,
            notes=TEST_RECORD_MARKER,
        ),
    )
    await ensure_portals_seeded(db_session, seeds=seeds)
    portal = await db_session.scalar(
        select(Portal).where(Portal.slug == seeds[0].slug)
    )
    assert portal is not None
    portal.is_enabled = False
    await db_session.flush()

    await ensure_portals_seeded(db_session, seeds=seeds)
    await db_session.refresh(portal)

    assert portal.is_enabled is False


def test_only_approved_portals_are_seeded() -> None:
    # Jobright in Phase 4, Hiring Cafe in Phase 9A, SimplyHired in Phase 9C,
    # Greenhouse in Phase 9D, Scale.jobs in Phase 9E, Dayforce in Phase 9F,
    # Google Careers in Phase 9G, Lever in Phase 9H, Ashby in Phase 9I. Portals are added one at a
    # time; an unexpected slug means that gate was skipped. LinkedIn and ZipRecruiter
    # were removed in D-005 and must not reappear here.
    #
    # career_pages is the one row here that is not a job board: it is a career
    # source, returning companies rather than listings, and it is backed by
    # app.careers rather than by a portal adapter. It seeds through the same
    # table because that table is what the dashboard renders its checkboxes
    # from.
    assert {seed.slug for seed in PORTAL_SEEDS} == {
        "jobright",
        "hiringcafe",
        "simplyhired",
        "greenhouse",
        "scalejobs",
        "dayforce",
        "google_careers",
        "lever",
        "ashby",
        "workday",
        "smartrecruiters",
        "career_pages",
    }


def test_hiring_cafe_is_seeded_as_enabled_and_implemented() -> None:
    seed = next(seed for seed in PORTAL_SEEDS if seed.slug == "hiringcafe")

    assert seed.is_enabled is True
    # The notes must say where collection is allowed to read from, because the
    # site's own search surface is disallowed by robots.txt.
    assert "robots" in seed.notes.lower()
    assert "searchstate" in seed.notes.lower()


# --- Probe endpoint ---------------------------------------------------------


async def test_probing_jobright_returns_normalized_listings(
    api_client: AsyncClient, db_session: AsyncSession, stub_jobright
) -> None:
    await ensure_portals_seeded(db_session)

    response = await api_client.post(
        f"{BASE}/jobright/probe",
        json={"queries": ["Financial Analyst"], "limit": 3},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True
    assert data["collected"] > 0

    job = data["jobs"][0]
    assert job["source"] == "jobright"
    assert job["title"]
    # Every row must give the user something to open.
    assert job["application_url"].startswith("http")


async def test_the_probe_writes_nothing_to_the_database(
    api_client: AsyncClient, db_session: AsyncSession, stub_jobright
) -> None:
    await ensure_portals_seeded(db_session)

    await api_client.post(
        f"{BASE}/jobright/probe", json={"queries": ["Financial Analyst"], "limit": 3}
    )

    # A probe is a diagnostic: no search run, no jobs, no application state.
    assert await db_session.scalar(select(func.count()).select_from(SearchRun)) == 0
    from app.models.job import Job

    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0


async def test_a_portal_refusing_access_is_reported_not_raised(
    api_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal is a successful diagnostic, not a 500.

    No portal refuses before transport any more - the two that did were
    removed in D-005 - so the refusal is induced here rather than borrowed
    from a portal's real state. The guarantee under test is the endpoint's,
    not any one portal's.
    """
    await ensure_portals_seeded(db_session)

    async def _blocked(self, url, params=None):  # noqa: ANN001
        raise PortalBlockedError(
            "refused access (robots.txt)", portal="jobright"
        )

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _blocked)

    response = await api_client.post(
        f"{BASE}/jobright/probe", json={"queries": ["Financial Analyst"], "limit": 3}
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is False
    assert data["error_code"] == PortalBlockedError.code
    assert "robots.txt" in data["error_message"]
    assert data["jobs"] == []


async def test_a_portal_failure_is_reported_not_raised(
    api_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolation rule, seen from the API: one portal breaking is data."""
    await ensure_portals_seeded(db_session)

    async def _timeout(self, url, params=None):  # noqa: ANN001
        raise PortalTimeoutError("timed out", portal="jobright")

    async def _head_ok(self, url):  # noqa: ANN001
        return True

    monkeypatch.setattr("app.portals.http.PortalHttpClient.get_text", _timeout)
    monkeypatch.setattr("app.portals.http.PortalHttpClient.head_ok", _head_ok)

    response = await api_client.post(
        f"{BASE}/jobright/probe", json={"queries": ["Financial Analyst"], "limit": 3}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ok"] is False
    assert data["error_code"] == PortalTimeoutError.code


async def test_an_unknown_portal_is_404(api_client: AsyncClient) -> None:
    response = await api_client.post(
        f"{BASE}/not-a-portal/probe", json={"queries": ["Analyst"]}
    )

    assert response.status_code == 404


async def test_the_limit_is_capped_server_side(
    api_client: AsyncClient, db_session: AsyncSession, stub_jobright
) -> None:
    # A probe must stay a light touch on the portal.
    await ensure_portals_seeded(db_session)

    response = await api_client.post(
        f"{BASE}/jobright/probe", json={"queries": ["Financial Analyst"], "limit": 999}
    )

    assert response.status_code == 422


async def test_at_least_one_query_is_required(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await ensure_portals_seeded(db_session)

    response = await api_client.post(f"{BASE}/jobright/probe", json={"queries": []})

    assert response.status_code == 422


async def test_portals_are_listed_with_their_selectable_state(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    await ensure_portals_seeded(db_session)

    response = await api_client.get(BASE)

    assert response.status_code == 200
    by_slug = {item["slug"]: item for item in response.json()["data"]["items"]}
    assert by_slug["jobright"]["is_implemented"] is True
    assert by_slug["jobright"]["is_selectable"] is True

    # And the other branch: implemented, but switched off by an operator, so
    # the checkbox must not offer it.
    from tests.factories import make_portal

    disabled = await make_portal(
        db_session, slug="switched-off", is_implemented=True, is_enabled=False
    )
    refreshed = await api_client.get(BASE)
    rows = {item["slug"]: item for item in refreshed.json()["data"]["items"]}
    assert rows[disabled.slug]["is_implemented"] is True
    assert rows[disabled.slug]["is_selectable"] is False
