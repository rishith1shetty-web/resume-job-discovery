"""Portal registry contract.

These tests protect the isolation rule: portals are resolved through the
registry, never named in core code.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.portals.base import (
    BasePortal,
    PortalCapabilities,
    PortalSearchRequest,
    PortalSearchResult,
    RawJob,
)
from app.portals.exceptions import PortalNotRegisteredError
from app.portals.registry import PortalRegistry, registry


class FakePortal(BasePortal):
    """Test double. Real adapters arrive in Phase 4."""

    slug = "fake"
    display_name = "Fake Portal"

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(supports_date_filter=True)

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        return PortalSearchResult(jobs=[], window=request.window)

    async def fetch_description(self, job: RawJob) -> str | None:
        return None

    async def validate_url(self, url: str) -> bool:
        return True


class OtherFakePortal(FakePortal):
    slug = "other-fake"
    display_name = "Other Fake Portal"


@pytest.fixture
def empty_registry() -> PortalRegistry:
    return PortalRegistry()


def test_the_global_registry_holds_only_the_approved_adapters() -> None:
    # Phase 4 registered Jobright and LinkedIn, in the rollout order recorded
    # in docs/DECISIONS.md (D-001); Phase 9A added Hiring Cafe, Phase 9B
    # ZipRecruiter and Phase 9C SimplyHired. Portals are added one at a time,
    # so an unexpected slug here means that gate was skipped.
    import app.portals.adapters  # noqa: F401  - registers the adapters

    assert set(registry.slugs) == {
        "jobright",
        "linkedin",
        "hiringcafe",
        "ziprecruiter",
        "simplyhired",
    }


def test_register_and_create(empty_registry: PortalRegistry) -> None:
    empty_registry.register(FakePortal)

    assert "fake" in empty_registry
    instance = empty_registry.create("fake", {"board_token": "abc"})

    assert isinstance(instance, BasePortal)
    assert instance.options == {"board_token": "abc"}


def test_register_works_as_decorator(empty_registry: PortalRegistry) -> None:
    returned = empty_registry.register(FakePortal)

    assert returned is FakePortal


def test_duplicate_slug_is_rejected(empty_registry: PortalRegistry) -> None:
    empty_registry.register(FakePortal)

    class Clashing(FakePortal):
        pass

    with pytest.raises(ValueError, match="already registered"):
        empty_registry.register(Clashing)


def test_registering_same_class_twice_is_idempotent(
    empty_registry: PortalRegistry,
) -> None:
    empty_registry.register(FakePortal)
    empty_registry.register(FakePortal)

    assert len(empty_registry) == 1


def test_adapter_without_slug_is_rejected(empty_registry: PortalRegistry) -> None:
    class Slugless(FakePortal):
        slug = ""

    with pytest.raises(ValueError, match="non-empty"):
        empty_registry.register(Slugless)


def test_unknown_slug_raises_portal_error(empty_registry: PortalRegistry) -> None:
    with pytest.raises(PortalNotRegisteredError) as exc_info:
        empty_registry.get("linkedin")

    assert exc_info.value.portal == "linkedin"


def test_create_many_preserves_caller_order(empty_registry: PortalRegistry) -> None:
    empty_registry.register(FakePortal)
    empty_registry.register(OtherFakePortal)

    instances = list(empty_registry.create_many(["other-fake", "fake"]))

    # The search service iterates the user's selection; order must be honoured.
    assert [type(i).slug for i in instances] == ["other-fake", "fake"]


def test_create_many_passes_per_portal_options(empty_registry: PortalRegistry) -> None:
    empty_registry.register(FakePortal)
    empty_registry.register(OtherFakePortal)

    instances = list(
        empty_registry.create_many(
            ["fake", "other-fake"],
            options_by_slug={"fake": {"timeout": 10}},
        )
    )

    assert instances[0].options == {"timeout": 10}
    assert instances[1].options == {}


def test_unregister(empty_registry: PortalRegistry) -> None:
    empty_registry.register(FakePortal)
    empty_registry.unregister("fake")

    assert "fake" not in empty_registry


async def test_default_health_check_is_available() -> None:
    health = await FakePortal().health_check()

    assert health.is_available is True
    assert health.checked_at <= datetime.now(UTC)


def test_base_portal_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BasePortal()  # type: ignore[abstract]


def test_raw_job_matches_the_specified_contract() -> None:
    job = RawJob(
        external_id="123",
        source="fake",
        title="Financial Analyst",
        company="Acme",
        location="Remote",
        description=None,
        source_url="https://example.com/jobs/123",
        application_url=None,
        posted_at=None,
        employment_type="full_time",
    )

    assert job.raw_payload == {}
    # Field set is fixed by CLAUDE.md section 4; adapters may not extend it.
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
