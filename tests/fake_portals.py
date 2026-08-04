"""Deterministic portal adapters for testing the search orchestration.

Only one live portal exists today, so fair multi-portal allocation, shortage
redistribution and partial-failure isolation cannot be demonstrated against
real portals. These adapters stand in for that: they return fixed listings with
no network access, so a test asserts the orchestration rule rather than a
portal's current inventory.

Nothing here is a claim about a real portal. A test built on these fixtures
verifies the allocation logic and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from app.portals.base import (
    BasePortal,
    PortalCapabilities,
    PortalSearchRequest,
    PortalSearchResult,
    RawJob,
)
from app.portals.registry import PortalRegistry

# Long enough to be scoreable (Phase 6 refuses to score a teaser) and written
# so a Financial Analyst resume clears the 70% threshold against it.
FINANCE_DESCRIPTION = """About the role
We are hiring a Financial Analyst to own forecasting and the monthly close for
a growing finance team.

Requirements:
- 3+ years of experience in financial analysis
- Advanced Excel
- SQL
- Forecasting and budgeting

Preferred qualifications:
- Power BI
- Variance analysis

The role reports into the FP&A lead and partners with business units on
planning, reporting and month-end close activities.
"""

# Same length and shape, but a different profession: the role gate must reject
# it however well the skills overlap.
ACCOUNTING_DESCRIPTION = FINANCE_DESCRIPTION.replace(
    "Financial Analyst", "Accountant"
)


def make_raw_job(
    *,
    portal: str,
    index: int = 0,
    title: str = "Financial Analyst",
    company: str | None = None,
    location: str = "New York, NY",
    description: str | None = None,
    age_days: float = 0.0,
    external_id: str | None = None,
    source_url: str | None = None,
    application_url: str | None = None,
    now: datetime | None = None,
) -> RawJob:
    """One listing as a portal would return it.

    ``company`` defaults to a per-listing value so listings stay distinct; pass
    the same company and title from two portals to exercise deduplication.
    """
    moment = now or datetime.now(UTC)
    resolved_company = company if company is not None else f"Company {index}"
    slugged = resolved_company.lower().replace(" ", "-")

    return RawJob(
        external_id=external_id or f"{portal}-{index}",
        source=portal,
        title=title,
        company=resolved_company,
        location=location,
        description=description if description is not None else FINANCE_DESCRIPTION,
        source_url=source_url or f"https://{portal}.invalid/jobs/{slugged}-{index}",
        application_url=(
            application_url or f"https://{portal}.invalid/jobs/{slugged}-{index}"
        ),
        posted_at=moment - timedelta(days=age_days),
        employment_type="Full-time",
        raw_payload={"portal": portal, "index": index},
    )


def make_jobs(
    portal: str,
    count: int,
    *,
    title: str = "Financial Analyst",
    description: str | None = None,
    age_days: float = 0.0,
    now: datetime | None = None,
) -> list[RawJob]:
    """``count`` distinct listings from one portal.

    The employer name carries the portal, so two portals built this way produce
    genuinely different vacancies. Without that they would be identical
    postings and Phase 5 would - correctly - merge them into one canonical job,
    which is the opposite of what a fair-allocation test wants. Pass an explicit
    ``company`` to ``make_raw_job`` when you *do* want a cross-portal duplicate.
    """
    return [
        make_raw_job(
            portal=portal,
            index=index,
            title=title,
            company=f"{portal.title()} Company {index}",
            description=description,
            age_days=age_days,
            now=now,
        )
        for index in range(count)
    ]


class FakePortal(BasePortal):
    """A portal that returns a fixed list, or raises a fixed error.

    ``jobs_by_window`` lets a test drive the freshness ladder explicitly. When
    it is not given, every window sees the same ``jobs`` list and the local
    ``posted_at`` filter decides - which is how a portal with no date filter
    behaves in production.
    """

    slug: ClassVar[str] = "fake"
    display_name: ClassVar[str] = "Fake Portal"

    #: Set per subclass.
    jobs: ClassVar[list[RawJob]] = []
    error: ClassVar[Exception | None] = None
    date_filter: ClassVar[bool] = False

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        #: Every request this adapter received, so a test can assert that a
        #: window was not collected twice.
        self.calls: list[PortalSearchRequest] = []
        type(self).instances.append(self)

    instances: ClassVar[list[FakePortal]] = []

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            supports_date_filter=type(self).date_filter,
            supports_description_fetch=False,
            supports_pagination=False,
            requires_auth=False,
            provides_application_url=True,
        )

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        self.calls.append(request)

        if type(self).error is not None:
            raise type(self).error

        return PortalSearchResult(
            jobs=list(type(self).jobs)[: request.limit],
            window=request.window,
            has_more=False,
            window_applied_by_portal=type(self).date_filter,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        return job.description

    async def validate_url(self, url: str) -> bool:
        return True


def build_fake_portal(
    slug: str,
    *,
    jobs: list[RawJob] | None = None,
    error: Exception | None = None,
    date_filter: bool = False,
    display_name: str | None = None,
) -> type[FakePortal]:
    """Create a fresh adapter class for one test."""
    return type(
        f"FakePortal_{slug.replace('-', '_')}",
        (FakePortal,),
        {
            "slug": slug,
            "display_name": display_name or f"Fake {slug}",
            "jobs": list(jobs or []),
            "error": error,
            "date_filter": date_filter,
            "instances": [],
        },
    )


class temporary_registry:
    """Register adapters on the process registry, then remove them again.

    The API goes through the process-wide registry, so an API test has to put
    its fakes there. Removing them in ``__exit__`` keeps tests independent of
    each other's portals.
    """

    def __init__(
        self, registry: PortalRegistry, *adapters: type[BasePortal]
    ) -> None:
        self._registry = registry
        self._adapters = adapters

    def __enter__(self) -> temporary_registry:
        for adapter in self._adapters:
            self._registry.register(adapter)
        return self

    def __exit__(self, *exc_info: object) -> None:
        for adapter in self._adapters:
            self._registry.unregister(adapter.slug)


__all__ = [
    "ACCOUNTING_DESCRIPTION",
    "FINANCE_DESCRIPTION",
    "FakePortal",
    "build_fake_portal",
    "make_jobs",
    "make_raw_job",
    "temporary_registry",
]
