"""Driving one portal adapter through the freshness ladder.

Collection only. Nothing here normalises, scores, deduplicates against the
database or knows which portal it is talking to - it holds a ``BasePortal`` and
reads its declared capabilities.

# Not re-collecting the same window

Portals differ in what they can filter. ``PortalCapabilities.supports_date_filter``
decides the strategy:

* **The portal filters by date.** Each window issues its own request, because
  each returns different listings.
* **The portal does not.** One request is issued for the whole ladder and the
  windows are applied locally to what came back. Re-requesting would return
  byte-identical results and cost the portal a request for nothing. Jobright is
  in this group: its taxonomy pages carry no date filter and ignore ``?page=``,
  so a second call yields the same page again.

Either way a listing is yielded **once**: the collector tracks what it has
already handed out, so a widening window delivers only what it newly admits.
The caller therefore never processes the same listing twice.

# Isolation

Every adapter call is wrapped in a timeout and every failure is raised as a
``PortalError``, which the orchestrator records against this portal's leg and
no other (CLAUDE.md section 18).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.constants import FRESHNESS_ESCALATION, FreshnessWindow
from app.core.logging import get_logger
from app.portals.base import BasePortal, PortalSearchRequest, RawJob
from app.portals.exceptions import PortalError, PortalTimeoutError
from app.search.freshness import within_window

logger = get_logger(__name__)

#: Default ceiling on one adapter call, when the portal config sets none.
DEFAULT_QUERY_TIMEOUT_SECONDS = 30.0

#: Default ceiling on everything one portal is allowed to spend in a run. A
#: synchronous request must return, so a slow portal is cut off rather than
#: allowed to hold the whole search open.
DEFAULT_PORTAL_BUDGET_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ExecutedQuery:
    """One query actually issued, for the ``search_queries`` audit trail."""

    query_text: str
    window: FreshnessWindow
    page: int
    result_count: int
    executed_at: datetime
    duration_ms: int


@dataclass(slots=True)
class WindowBatch:
    """Listings newly admitted by one window.

    ``executed`` is empty when the window was served from an earlier fetch -
    which is the normal case for a portal that cannot filter by date, and the
    evidence that no redundant request was made.
    """

    window: FreshnessWindow
    jobs: list[RawJob] = field(default_factory=list)
    executed: list[ExecutedQuery] = field(default_factory=list)

    @property
    def fetched(self) -> bool:
        return bool(self.executed)


def _identity(job: RawJob) -> str:
    """Stable per-portal identity for a collected listing.

    Only used to avoid handing the same listing to the caller twice within one
    portal's collection. Cross-portal identity is a much harder question and is
    settled by Phase 5's canonical deduplication, not here.
    """
    return job.external_id or job.source_url


class PortalCollector:
    """Collects one portal's listings, window by window.

    Constructed per portal per run. Not reusable across runs: it remembers what
    it has already yielded.
    """

    def __init__(
        self,
        adapter: BasePortal,
        *,
        queries: Sequence[str],
        per_window_limit: int,
        query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
        budget_seconds: float = DEFAULT_PORTAL_BUDGET_SECONDS,
    ) -> None:
        self._adapter = adapter
        self._queries = tuple(queries)
        self._per_window_limit = max(per_window_limit, 1)
        self._query_timeout = query_timeout_seconds
        self._budget = budget_seconds

        self._yielded: set[str] = set()
        #: Listings fetched once and filtered locally, for portals that cannot
        #: filter by date themselves.
        self._pool: list[RawJob] = []
        self._pool_loaded = False
        self._started_at = 0.0

    @property
    def budget_exhausted(self) -> bool:
        if self._budget <= 0:
            return False
        return (time.monotonic() - self._started_at) >= self._budget

    async def windows(
        self,
        *,
        windows: Sequence[FreshnessWindow] = FRESHNESS_ESCALATION,
        now: datetime,
    ) -> AsyncIterator[WindowBatch]:
        """Yield one batch per window, newest first.

        The caller decides when to stop - typically as soon as the portal's
        quota is filled with qualified jobs, which is what keeps the search
        from widening unnecessarily (CLAUDE.md section 6).
        """
        self._started_at = time.monotonic()

        if not self._queries:
            return

        filters_by_date = self._adapter.capabilities.supports_date_filter

        for window in windows:
            if self.budget_exhausted:
                logger.info(
                    "portal_budget_exhausted",
                    portal=self._adapter.slug,
                    window=window.value,
                )
                return

            if filters_by_date:
                batch = await self._fetch_window(window, now=now)
            else:
                batch = await self._from_pool(window, now=now)

            yield batch

    # --- Strategies --------------------------------------------------------

    async def _fetch_window(
        self, window: FreshnessWindow, *, now: datetime
    ) -> WindowBatch:
        """Ask the portal for this window directly."""
        batch = WindowBatch(window=window)

        for query in self._queries:
            if len(batch.jobs) >= self._per_window_limit or self.budget_exhausted:
                break

            collected, executed = await self._search(query, window)
            batch.executed.append(executed)

            for job in collected:
                if len(batch.jobs) >= self._per_window_limit:
                    break
                if self._admit(job):
                    batch.jobs.append(job)

        return batch

    async def _from_pool(
        self, window: FreshnessWindow, *, now: datetime
    ) -> WindowBatch:
        """Filter one already-collected pool by date, locally.

        The pool is fetched on the first window only. Later windows re-filter
        it and issue no request at all.
        """
        batch = WindowBatch(window=window)

        if not self._pool_loaded:
            self._pool_loaded = True
            for query in self._queries:
                if self.budget_exhausted:
                    break
                collected, executed = await self._search(query, window)
                batch.executed.append(executed)
                self._pool.extend(collected)

        for job in self._pool:
            if len(batch.jobs) >= self._per_window_limit:
                break
            if not within_window(job.posted_at, window, now):
                continue
            if self._admit(job):
                batch.jobs.append(job)

        return batch

    # --- Adapter plumbing --------------------------------------------------

    async def _search(
        self, query: str, window: FreshnessWindow
    ) -> tuple[list[RawJob], ExecutedQuery]:
        """One adapter call, timed and bounded.

        A timeout is reported as a portal failure rather than an empty result:
        an empty result would look like "this portal has no matching jobs",
        which is a materially different and misleading claim.
        """
        request = PortalSearchRequest(
            queries=(query,), window=window, limit=self._per_window_limit
        )

        started = time.monotonic()

        try:
            result = await asyncio.wait_for(
                self._adapter.search(request), timeout=self._query_timeout
            )
        except TimeoutError as exc:
            raise PortalTimeoutError(
                f"{self._adapter.slug} did not respond within "
                f"{self._query_timeout:g}s for query {query!r}.",
                portal=self._adapter.slug,
            ) from exc
        except PortalError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            # An adapter is expected to raise PortalError. Anything else is a
            # bug in that adapter, and it must still stay isolated to it.
            raise PortalError(
                f"{self._adapter.slug} failed unexpectedly: {exc}",
                portal=self._adapter.slug,
            ) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        jobs = list(result.jobs)

        logger.info(
            "portal_query_executed",
            portal=self._adapter.slug,
            query=query,
            window=window.value,
            returned=len(jobs),
            duration_ms=duration_ms,
        )

        return jobs, ExecutedQuery(
            query_text=query,
            window=window,
            page=1,
            result_count=len(jobs),
            executed_at=datetime.now(UTC),
            duration_ms=duration_ms,
        )

    def _admit(self, job: RawJob) -> bool:
        """True the first time this listing is seen in this run."""
        identity = _identity(job)
        if identity in self._yielded:
            return False
        self._yielded.add(identity)
        return True


__all__ = [
    "DEFAULT_PORTAL_BUDGET_SECONDS",
    "DEFAULT_QUERY_TIMEOUT_SECONDS",
    "ExecutedQuery",
    "PortalCollector",
    "WindowBatch",
]
