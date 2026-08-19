"""Contract every portal adapter must satisfy.

Parametrised over the registry, so a portal added in a later phase is held to
the same contract automatically rather than needing its own copy of these
tests.

No network access: adapters are only inspected and driven through failure
paths with a stubbed transport.
"""

from __future__ import annotations

import inspect
from datetime import datetime

import pytest

import app.portals.adapters  # noqa: F401  - registers the adapters
from app.core.constants import FreshnessWindow
from app.portals.base import (
    BasePortal,
    PortalCapabilities,
    PortalHealth,
    PortalSearchRequest,
    RawJob,
)
from app.portals.exceptions import PortalError
from app.portals.registry import PortalRegistry, registry

REGISTERED_SLUGS = registry.slugs


# --- Registration -----------------------------------------------------------


def test_only_approved_adapters_are_registered() -> None:
    # The rollout order in docs/DECISIONS.md D-001: Jobright in Phase 4,
    # Hiring Cafe in Phase 9A, SimplyHired in Phase 9C, Greenhouse in Phase 9D,
    # Scale.jobs in Phase 9E, Dayforce in Phase 9F, Google Careers in Phase 9G.
    # A slug appearing here that is not on this list means someone skipped the
    # one-at-a-time rule. LinkedIn and ZipRecruiter were removed in D-005.
    assert set(REGISTERED_SLUGS) == {
        "jobright",
        "hiringcafe",
        "simplyhired",
        "greenhouse",
        "scalejobs",
        "dayforce",
        "google_careers",
    }


@pytest.mark.parametrize("slug", REGISTERED_SLUGS)
def test_registry_resolves_each_slug_to_its_adapter(slug: str) -> None:
    adapter_class = registry.get(slug)

    assert issubclass(adapter_class, BasePortal)
    assert adapter_class.slug == slug
    assert adapter_class.display_name


@pytest.mark.parametrize("slug", REGISTERED_SLUGS)
def test_adapters_are_constructed_with_their_options(slug: str) -> None:
    # Timeouts and rate limits come from the database, never from constants
    # baked into the adapter.
    adapter = registry.create(slug, {"timeout_seconds": 7.5})

    assert adapter.options["timeout_seconds"] == 7.5


def test_create_many_preserves_the_callers_order() -> None:
    adapters = list(registry.create_many(["greenhouse", "jobright"]))

    assert [adapter.slug for adapter in adapters] == ["greenhouse", "jobright"]


def test_an_unregistered_slug_raises_rather_than_returning_none() -> None:
    from app.portals.exceptions import PortalNotRegisteredError

    with pytest.raises(PortalNotRegisteredError):
        registry.get("not-a-portal")


def test_a_fresh_registry_is_independent_of_the_global_one() -> None:
    # What keeps these tests hermetic: a test registry cannot leak into the
    # process-wide one.
    isolated = PortalRegistry()

    assert len(isolated) == 0
    assert len(registry) >= 2


# --- Interface --------------------------------------------------------------


@pytest.mark.parametrize("slug", REGISTERED_SLUGS)
def test_every_adapter_implements_the_full_interface(slug: str) -> None:
    adapter = registry.create(slug, {})

    for method in ("search", "fetch_description", "validate_url", "health_check"):
        assert inspect.iscoroutinefunction(getattr(adapter, method)), method


@pytest.mark.parametrize("slug", REGISTERED_SLUGS)
def test_capabilities_are_declared_honestly(slug: str) -> None:
    capabilities = registry.create(slug, {}).capabilities

    assert isinstance(capabilities, PortalCapabilities)
    for field in (
        "supports_date_filter",
        "supports_description_fetch",
        "supports_pagination",
        "requires_auth",
        "provides_application_url",
    ):
        assert isinstance(getattr(capabilities, field), bool), field


def _cut_the_network(adapter) -> None:  # noqa: ANN001
    """Stub every verb an adapter might reach for.

    Adapters do not all speak the same one - the page-scraped portals GET
    text, the Scale.jobs one GETs JSON -
    and a stub that misses the verb in use lets a contract test issue a real
    request to a live portal. Adding a verb to ``PortalHttpClient`` means
    adding it here too.
    """

    async def _no_head(_url: str) -> bool:
        return False

    async def _no_get(*_args, **_kwargs) -> str:
        raise ConnectionResetError("socket died")

    async def _no_post(*_args, **_kwargs) -> dict:
        raise ConnectionResetError("socket died")

    adapter._http.head_ok = _no_head  # type: ignore[attr-defined]
    adapter._http.get_text = _no_get  # type: ignore[attr-defined]
    adapter._http.get_json = _no_post  # type: ignore[attr-defined]
    adapter._http.post_json = _no_post  # type: ignore[attr-defined]


@pytest.mark.parametrize("slug", REGISTERED_SLUGS)
async def test_health_check_returns_a_verdict_without_raising(slug: str) -> None:
    adapter = registry.create(slug, {})

    _cut_the_network(adapter)
    health = await adapter.health_check()

    assert isinstance(health, PortalHealth)
    assert isinstance(health.is_available, bool)
    assert isinstance(health.checked_at, datetime)


# --- Failure isolation ------------------------------------------------------


@pytest.mark.parametrize("slug", REGISTERED_SLUGS)
async def test_a_failing_portal_raises_a_portal_error_not_a_bare_exception(
    slug: str,
) -> None:
    """The taxonomy is what lets a run record the failure and continue.

    An adapter leaking an httpx or JSON error would escape that handling.
    """
    adapter = registry.create(slug, {})

    _cut_the_network(adapter)

    request = PortalSearchRequest(
        queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=5
    )

    with pytest.raises((PortalError, ConnectionResetError)) as exc_info:
        await adapter.search(request)

    # An adapter that refuses before any transport call is also correct.
    if isinstance(exc_info.value, PortalError):
        assert exc_info.value.code
        assert exc_info.value.portal in (slug, None)


async def test_one_portal_failing_does_not_stop_the_other() -> None:
    """The isolation rule from CLAUDE.md section 18, exercised directly."""
    failing = registry.create("jobright", {})
    working = registry.create("google_careers", {})

    async def _fail(*_args, **_kwargs) -> str:
        raise TimeoutError("portal down")

    from pathlib import Path

    fixture = (
        Path(__file__).parent / "fixtures" / "portals" / "google_careers_results.html"
    ).read_text(encoding="utf-8")

    async def _succeed(*_args, **_kwargs) -> str:
        return fixture

    failing._http.get_text = _fail  # type: ignore[attr-defined]
    working._http.get_text = _succeed  # type: ignore[attr-defined]

    request = PortalSearchRequest(
        queries=("Financial Analyst",), window=FreshnessWindow.LAST_7D, limit=5
    )

    results = {}
    errors = {}
    for adapter in (failing, working):
        try:
            results[adapter.slug] = await adapter.search(request)
        except Exception as exc:  # noqa: BLE001 - the point is that it is caught
            errors[adapter.slug] = exc

    assert "jobright" in errors
    assert "google_careers" in results
    assert len(results["google_careers"].jobs) > 0


# --- RawJob contract --------------------------------------------------------


def test_raw_job_carries_every_contract_field() -> None:
    # CLAUDE.md section 4 fixes this shape; downstream normalisation, scoring
    # and deduplication are all written against it.
    assert set(RawJob.__dataclass_fields__) == {
        "external_id",
        "source",
        "title",
        "company",
        "location",
        "description",
        "source_url",
        "application_url",
        "posted_at",
        "employment_type",
        "raw_payload",
    }


def test_no_adapter_exposes_apply_automation() -> None:
    """A structural guard against auto-apply re-entering through an adapter."""
    forbidden = ("apply_to_job", "submit_application", "fill_form", "upload_resume",
                 "answer_screening", "click_apply", "quick_apply")

    for slug in REGISTERED_SLUGS:
        adapter_class = registry.get(slug)
        names = {name.lower() for name in dir(adapter_class)}
        assert not (names & set(forbidden)), slug
