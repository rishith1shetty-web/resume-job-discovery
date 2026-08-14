"""The built-in career-page source, and the collection pass it can fall back on.

# Where its companies come from

From live postings, in two ways, in this order:

1. **The run's own collection.** When the user ticks Career Pages alongside
   Jobright or Hiring Cafe, those portals have already returned listings by the
   time this runs. Reusing them costs nothing - no extra request, no extra
   wait - and it is the same evidence the job results were built from.

2. **Its own pass, when it is the only source selected.** Career Pages has to
   work on its own, so with nothing to reuse it collects for itself through the
   portals that are already enabled, shallowly: it needs employers and their
   application links, not depth. That is reuse of ``PortalCollector`` and the
   existing adapters, not a second search engine.

Either way the companies are whoever is actually hiring for the resume's role
in the United States right now. No company list is maintained anywhere in this
package.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from app.careers.base import (
    CareerPageDiscovery,
    CareerPageRequest,
    CareerPageSource,
    career_registry,
)
from app.careers.discovery import MAX_CAREER_PAGES, discover_career_pages
from app.core.logging import get_logger
from app.portals.base import BasePortal, RawJob
from app.search.collection import PortalCollector
from app.search.depth import SearchDepth

logger = get_logger(__name__)

#: Listings requested per portal during a fallback pass. Enough employers to
#: fill twenty cards several times over, without the depth a job search needs:
#: this pass is looking for *who* is hiring, and a company is established by
#: one qualifying opening as firmly as by ten.
DEFAULT_DISCOVERY_LIMIT = 60

#: Seconds one fallback pass may spend on one portal. Kept tight because this
#: only runs when Career Pages was selected alone, and a supplementary result
#: category must not make a search feel broken.
DEFAULT_DISCOVERY_BUDGET_SECONDS = 45.0


async def collect_discovery_listings(
    adapters: Sequence[BasePortal],
    *,
    queries: tuple[str, ...],
    now: datetime,
    per_portal_limit: int = DEFAULT_DISCOVERY_LIMIT,
    budget_seconds: float = DEFAULT_DISCOVERY_BUDGET_SECONDS,
) -> list[RawJob]:
    """Collect listings purely to discover employers.

    Failure-isolated the same way the job pipeline is: one adapter raising
    costs that adapter's employers, not the pass. Nothing is written, scored or
    deduplicated here - the listings exist only to be grouped into companies.
    """
    if not adapters or not queries:
        return []

    # Explicitly shallow. A job search paginates to find the best twenty
    # vacancies; this only needs to learn who is hiring, and one page per
    # window establishes an employer as well as five would.
    depth = SearchDepth(
        max_pages=1,
        max_requests=max(1, len(queries)),
        max_runtime_seconds=budget_seconds,
        max_jobs=per_portal_limit,
        per_request_limit=per_portal_limit,
    )

    async def collect(adapter: BasePortal) -> list[RawJob]:
        collector = PortalCollector(
            adapter,
            queries=queries,
            per_window_limit=per_portal_limit,
            budget_seconds=budget_seconds,
        )
        try:
            collected = await collector.collect(now=now, depth=depth)
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            logger.info(
                "career_discovery_portal_failed",
                portal=getattr(adapter, "slug", "unknown"),
                error=str(exc),
            )
            return []
        return list(collected.jobs)

    batches = await asyncio.gather(*(collect(adapter) for adapter in adapters))
    return [job for batch in batches for job in batch]


@career_registry.register
class LiveListingCareerSource(CareerPageSource):
    """Career pages derived from openings that are live right now.

    Holds no company list and makes no request of its own: the orchestrator
    hands it listings, and it decides which employers among them publish a
    career page worth showing.
    """

    slug: ClassVar[str] = "career_pages"
    display_name: ClassVar[str] = "Career Pages"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._limit = self._configured_limit(self.options)

    @staticmethod
    def _configured_limit(options: dict[str, Any]) -> int:
        """Result ceiling, from configuration, never above the hard maximum.

        An operator may ask for fewer cards. Nobody can ask for more: the cap
        exists so this category stays supplementary, and a configuration value
        must not be able to turn it into the main result set.
        """
        configured = options.get("max_career_pages")
        if isinstance(configured, bool) or not isinstance(configured, int):
            return MAX_CAREER_PAGES
        return max(0, min(configured, MAX_CAREER_PAGES))

    async def discover(
        self,
        request: CareerPageRequest,
        *,
        listings: Sequence[Any],
        now: datetime,
    ) -> CareerPageDiscovery:
        if request.profile is None:
            # Without an analysed resume there is no role gate, and every
            # employer would look relevant. Returning nothing is the honest
            # answer; the run's own validation is what reports the cause.
            return CareerPageDiscovery()

        discovery = discover_career_pages(
            listings,
            profile=request.profile,
            request=request,
            now=now,
            limit=min(self._limit, request.limit or self._limit),
        )

        logger.info(
            "career_pages_discovered",
            source=self.slug,
            listings=len(listings),
            companies=discovery.companies_seen,
            returned=discovery.returned_count,
            unverified=discovery.unverified_companies,
            non_us=discovery.non_us_openings,
            off_role=discovery.off_role_openings,
        )
        return discovery


__all__ = [
    "DEFAULT_DISCOVERY_BUDGET_SECONDS",
    "DEFAULT_DISCOVERY_LIMIT",
    "LiveListingCareerSource",
    "collect_discovery_listings",
]
