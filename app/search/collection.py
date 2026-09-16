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

# Partial results

A deadline is not a failure. Ten of the eleven adapters cannot filter by date,
so the whole ladder is served from one pool that is loaded on the *first*
window - which means every request a portal makes in a run is made inside that
first pass. A timeout anywhere in it used to discard the pass, and with it the
listings the earlier queries had already returned, leaving the leg to be
recorded as "failed, nothing collected". The portal had usually collected
plenty.

So a failure is now weighed against what is in hand:

* **Nothing collected** - raised, exactly as before. An empty result would read
  as "this portal has no matching jobs", which is a materially different claim.
* **Something collected** - the batch is yielded carrying the jobs *and* the
  error, the ladder stops, and the leg is completed with
  ``ExhaustionReason.TIMEOUT``. The run keeps the jobs and says the sweep was
  cut short, which is what actually happened.

A timeout is also not final within a window. The portals that fan out cache
their boards for the life of the adapter, so the query that follows a timed-out
one is usually served from that cache and costs almost nothing - abandoning the
leg would throw away work already paid for. Consecutive timeouts are capped and
the leg budget still bounds everything. A refusal is different and is never
retried: ``PortalRateLimitedError`` and ``PortalBlockedError`` stop the leg at
once, because continuing would be ignoring what the portal just said.

Prevention comes first, though. Each call is handed a ``Deadline``, so an
adapter that fans out over boards can stop before a request it has no time for
and return what it holds. Cancellation destroys a coroutine's local state; a
cooperative return does not.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.constants import FRESHNESS_ESCALATION, FreshnessWindow
from app.core.logging import get_logger
from app.portals.base import (
    BasePortal,
    Deadline,
    PortalSearchRequest,
    RawJob,
)
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.portals.http import use_deadline
from app.search.depth import ExhaustionReason, SearchDepth
from app.search.freshness import within_window

logger = get_logger(__name__)

#: Default ceiling on one adapter call, when the portal config sets none.
DEFAULT_QUERY_TIMEOUT_SECONDS = 30.0

#: Default ceiling on everything one portal is allowed to spend in a run. A
#: synchronous request must return, so a slow portal is cut off rather than
#: allowed to hold the whole search open.
DEFAULT_PORTAL_BUDGET_SECONDS = 120.0

#: Margin held back from an adapter's deadline so it can finish assembling and
#: return before the hard timeout cancels it. Capped as a fraction of the call's
#: own ceiling so that a deliberately tiny timeout does not arrive pre-expired.
ADAPTER_RETURN_RESERVE_SECONDS = 5.0
ADAPTER_RETURN_RESERVE_FRACTION = 0.1

#: Per-query timeouts tolerated in a row before the portal is treated as
#: unresponsive and the leg stops. Two, not one, because the portals that fan
#: out cache their boards: the query after a timed-out one is usually served
#: from that cache. Not more than two, because past that the evidence says the
#: portal is not answering and the budget is better left unspent.
MAX_CONSECUTIVE_TIMEOUTS = 2


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
    #: The failure that ended this window, when one did. The jobs above were
    #: collected before it and are kept: they are valid listings that were
    #: already paid for.
    error: PortalError | None = None
    #: True when an adapter returned early against its deadline rather than
    #: failing. Nothing went wrong; there was simply more to read.
    partial: bool = False
    #: A timeout that was tolerated so the next query could be tried. Held
    #: rather than raised, and settled once the window is over: if a later
    #: query succeeded it is discarded, and if none did it becomes ``error``.
    #: Without this a portal whose only query timed out would return an empty
    #: window and read as "no matching jobs".
    tolerated_error: PortalError | None = None
    #: Queries that completed and gave this window a definitive answer,
    #: including the ones whose answer was "nothing". This is what separates a
    #: portal that could not be reached from a portal that was reached and has
    #: no such jobs - without it, the second is reported as the first.
    answered: int = 0
    #: Queries abandoned to a tolerated timeout. Telemetry only - the gap
    #: between requests attempted and requests that answered.
    lost_queries: int = 0

    @property
    def fetched(self) -> bool:
        return bool(self.executed)

    @property
    def cut_short(self) -> bool:
        return self.error is not None or self.partial


@dataclass(slots=True)
class DeepCollection:
    """Everything one portal produced in a run, and why it stopped.

    Holds no database state and touches no session, which is what lets portals
    collect concurrently: the writing happens afterwards, one portal at a time,
    in the user's selection order.
    """

    portal_slug: str
    jobs: list[RawJob] = field(default_factory=list)
    executed: list[ExecutedQuery] = field(default_factory=list)
    deepest_window: FreshnessWindow | None = None
    reason: ExhaustionReason = ExhaustionReason.EXHAUSTED
    requests: int = 0
    pages: int = 0
    #: Queries abandoned to a tolerated timeout. The gap between requests
    #: attempted and requests that answered, which is what makes a partial
    #: result legible rather than mysteriously short.
    lost_queries: int = 0
    elapsed_seconds: float = 0.0
    #: Raised when the portal produced nothing at all. The caller marks the leg
    #: failed; a portal that failed *after* collecting keeps what it collected.
    error: Exception | None = None

    @property
    def is_complete(self) -> bool:
        return self.error is None and self.reason.is_complete

    @property
    def is_partial(self) -> bool:
        """Jobs were collected, but the portal had not finished giving them.

        The distinction the run summary needs: this is neither a clean sweep
        nor a failure, and reporting it as either would be untrue. ``MAX_JOBS``
        is excluded on purpose - stopping because the configured ceiling was
        reached is the limit working, not the portal being cut off.

        Deliberately not conditional on having collected something. A portal
        that ran out of time before its first board answered has collected
        nothing *yet*, which is a different statement from having nothing to
        give, and "complete" would be the wrong word for it.
        """
        if self.error is not None:
            return False
        return self.reason in {
            ExhaustionReason.TIMEOUT,
            ExhaustionReason.RATE_LIMITED,
            ExhaustionReason.ERROR,
        }


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
        #: Set once something has cut this leg short. It does not end the walk,
        #: it ends the *spending*: no further adapter call is made, but windows
        #: already paid for keep being filtered.
        self._stopped = False
        self._started_at = 0.0
        #: Adapter calls made. The closest proxy this layer has to load placed
        #: on the portal, and one of the limits that bounds a deep search.
        self._requests = 0
        #: Queries lost to a timeout across the whole leg. Counted here rather
        #: than only on the window, because a window that fails before it is
        #: yielded never reaches the caller to be counted - which is how a lost
        #: query used to be reported as zero lost queries.
        self._lost_queries = 0

    @property
    def requests(self) -> int:
        return self._requests

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

        The caller decides when to stop. Since Phase 12 that caller is
        ``collect``, which walks the whole ladder and stops only on one of the
        portal's own depth limits - never because some number of jobs has been
        reached. Newest-first still matters: it is the order listings enter the
        pool, and it is what a portal cut short by a limit will have collected.
        """
        self._started_at = time.monotonic()

        if not self._queries:
            return

        filters_by_date = self._adapter.capabilities.supports_date_filter

        for window in windows:
            if self._stopped and filters_by_date:
                # Being stopped ends the spending. For a portal that answers
                # each window with its own request, a wider window *is* more
                # spending, so there is nothing left to be had for free and the
                # walk ends here. A pool-based portal keeps going: its
                # remaining windows only re-filter listings already paid for.
                return

            if filters_by_date:
                batch = await self._fetch_window(window, now=now)
            else:
                batch = await self._from_pool(window, now=now)

            if batch.error is not None and not batch.jobs and not self._yielded:
                # Nothing was collected anywhere in this leg, so there is no
                # partial result to protect and no honest way to report this as
                # a search that found nothing. Raise, exactly as before.
                raise batch.error

            yield batch

            if self._stopped:
                continue

            if batch.cut_short:
                self._stopped = True
                logger.info(
                    "portal_window_cut_short",
                    portal=self._adapter.slug,
                    window=window.value,
                    collected=len(batch.jobs),
                    partial=batch.partial,
                    error=(
                        batch.error.message
                        if isinstance(batch.error, PortalError)
                        else None
                    ),
                )
            elif self.budget_exhausted:
                self._stopped = True
                logger.info(
                    "portal_budget_exhausted",
                    portal=self._adapter.slug,
                    window=window.value,
                )

    async def collect(
        self,
        *,
        now: datetime,
        depth: SearchDepth,
        windows: Sequence[FreshnessWindow] = FRESHNESS_ESCALATION,
    ) -> DeepCollection:
        """Collect everything this portal has, bounded only by its own limits.

        This is the Phase 12 entry point. It walks the whole freshness ladder
        rather than stopping once some number of jobs has been found: how many
        the run finally returns is decided globally, afterwards, from the
        combined pool. A portal that stops early here is a portal that hit one
        of its *own* limits, never one that was told another portal had enough.

        Nothing here writes to the database, so several of these may run at
        once.

        A portal that fails partway keeps what it already collected and records
        why it stopped. A portal that fails having collected nothing raises
        through ``error``, and the caller marks that leg failed - losing forty
        good listings because the forty-first request was rate-limited would be
        the wrong trade.
        """
        collected = DeepCollection(portal_slug=self._adapter.slug)
        started = time.monotonic()
        total_windows = len(windows)
        visited = 0
        #: Why the leg was cut short, and the failure behind it if there was
        #: one. Held rather than applied immediately: the windows that follow
        #: cost nothing and may still admit listings already in the pool.
        cut_reason: ExhaustionReason | None = None
        cut_error: PortalError | None = None

        try:
            async for batch in self.windows(windows=windows, now=now):
                visited += 1
                collected.deepest_window = batch.window
                collected.executed.extend(batch.executed)
                collected.requests = self._requests

                if batch.fetched:
                    collected.pages += 1

                truncated = False
                for job in batch.jobs:
                    if depth.job_budget_reached(len(collected.jobs)):
                        truncated = True
                        break
                    collected.jobs.append(job)

                if truncated:
                    collected.reason = ExhaustionReason.MAX_JOBS
                    break

                if batch.cut_short and cut_reason is None:
                    # Kept, not raised: these listings are valid and were paid
                    # for. The walk continues only over windows that cost
                    # nothing; `windows` has stopped all further spending.
                    cut_reason = (
                        _reason_for(batch.error)
                        if batch.error is not None
                        else ExhaustionReason.TIMEOUT
                    )
                    cut_error = batch.error

                # A limit only ended the leg early if there was more to do.
                # Walking the whole freshness ladder is not "hit the page cap";
                # it is the portal having nothing further to give.
                if visited >= total_windows:
                    break

                if collected.pages >= depth.max_pages:
                    collected.reason = ExhaustionReason.MAX_PAGES
                    break
                if depth.request_budget_reached(self._requests):
                    collected.reason = ExhaustionReason.MAX_REQUESTS
                    break
                if self.budget_exhausted and cut_reason is None:
                    # Recorded, not obeyed as a stop. A portal that spent its
                    # budget on the first window has still paid for a pool, and
                    # the windows that would admit anything older than a day
                    # are the ones that cost nothing. Ending the walk here was
                    # losing eligible listings for no saving at all.
                    cut_reason = ExhaustionReason.TIMEOUT

        except PortalError as exc:
            collected.reason = _reason_for(exc)
            collected.requests = self._requests
            if not collected.jobs:
                # Nothing survived, so there is no partial result to protect.
                collected.error = exc
        else:
            if cut_reason is not None:
                # Deliberately overrides a depth limit recorded above. If this
                # leg was both cut short *and* hit its job ceiling, the ceiling
                # is the proximate stop but the cut is the one that matters:
                # it is the reason coverage is incomplete, and reporting the
                # ceiling instead would describe a truncated sweep as a portal
                # that simply had plenty.
                collected.reason = cut_reason
                if not collected.jobs and cut_error is not None:
                    collected.error = cut_error

        collected.elapsed_seconds = round(time.monotonic() - started, 3)
        collected.requests = self._requests
        # Read from the collector rather than summed over the batches: a window
        # that raised instead of being yielded still lost its queries, and they
        # are exactly the ones worth reporting.
        collected.lost_queries = self._lost_queries

        logger.info(
            "portal_collection_finished",
            portal=self._adapter.slug,
            collected=len(collected.jobs),
            requests=collected.requests,
            pages=collected.pages,
            reason=collected.reason.value,
            partial=collected.is_partial,
            lost_queries=collected.lost_queries,
            deepest_window=(
                collected.deepest_window.value if collected.deepest_window else None
            ),
            elapsed_seconds=collected.elapsed_seconds,
            failed=collected.error is not None,
        )

        return collected

    # --- Strategies --------------------------------------------------------

    async def _fetch_window(
        self, window: FreshnessWindow, *, now: datetime
    ) -> WindowBatch:
        """Ask the portal for this window directly."""
        batch = WindowBatch(window=window)
        timeouts = 0

        for query in self._queries:
            if self._stopped:
                break
            if len(batch.jobs) >= self._per_window_limit or self.budget_exhausted:
                break

            try:
                collected, executed, partial, consulted = await self._search(query, window)
            except PortalError as exc:
                timeouts = self._absorb(batch, exc, timeouts=timeouts)
                if batch.error is not None:
                    break
                continue

            timeouts = 0
            # `answered` records that the portal was reached and gave this
            # window a real answer, including an answer of "nothing". An
            # adapter that could not express the query never asked, so its
            # empty result is not an answer and does not count. Kept separate
            # from clearing the tolerated error below, which only a non-empty
            # result may do: being reachable is not the same as having covered
            # the query that was lost.
            if consulted:
                batch.answered += 1
            # Cleared only by an answer that carried something. An empty
            # result is not evidence that the timed-out query would have been
            # empty too - several adapters return nothing for a query they
            # simply cannot express - and forgiving the timeout on that basis
            # would let the leg report "no matching jobs" for a question the
            # portal never answered.
            if collected:
                batch.tolerated_error = None
            batch.executed.append(executed)
            batch.partial = batch.partial or partial

            for job in collected:
                if len(batch.jobs) >= self._per_window_limit:
                    break
                if self._admit(job):
                    batch.jobs.append(job)

        self._settle(batch)
        return batch

    async def _from_pool(
        self, window: FreshnessWindow, *, now: datetime
    ) -> WindowBatch:
        """Filter one already-collected pool by date, locally.

        The pool is fetched on the first window only. Later windows re-filter
        it and issue no request at all.
        """
        batch = WindowBatch(window=window)

        # Loaded once, on the first window. Every later window re-filters the
        # same pool and issues no request at all - which is why being cut short
        # must not end the walk: the remaining windows are free, and they are
        # where a listing older than twenty-four hours is finally admitted.
        if not self._pool_loaded and not self._stopped:
            self._pool_loaded = True
            timeouts = 0
            for query in self._queries:
                if self.budget_exhausted:
                    break
                try:
                    collected, executed, partial, consulted = await self._search(query, window)
                except PortalError as exc:
                    timeouts = self._absorb(batch, exc, timeouts=timeouts)
                    if batch.error is not None:
                        break
                    continue

                timeouts = 0
                # `answered` records that the portal was reached and gave
                # this window a real answer, including an answer of "nothing".
                # An adapter that could not express the query never asked, so
                # its empty result is not an answer and does not count. Kept
                # separate from clearing the tolerated error below, which only
                # a non-empty result may do: being reachable is not the same
                # as having covered the query that was lost.
                if consulted:
                    batch.answered += 1
                # Cleared only by an answer that carried something. An empty
                # result is not evidence that the timed-out query would have been
                # empty too - several adapters return nothing for a query they
                # simply cannot express - and forgiving the timeout on that basis
                # would let the leg report "no matching jobs" for a question the
                # portal never answered.
                if collected:
                    batch.tolerated_error = None
                batch.executed.append(executed)
                batch.partial = batch.partial or partial
                self._pool.extend(collected)

        # Runs whether or not the loading loop completed. This is the line that
        # decides whether a portal that timed out on its fourth query keeps the
        # listings its first three returned.
        for job in self._pool:
            if len(batch.jobs) >= self._per_window_limit:
                break
            if not within_window(job.posted_at, window, now):
                continue
            if self._admit(job):
                batch.jobs.append(job)

        # Settled here, not in the loading loop: whether a tolerated timeout
        # mattered depends on what the pool turned out to hold.
        self._settle(batch)
        return batch

    # --- Adapter plumbing --------------------------------------------------

    def _absorb(
        self, batch: WindowBatch, exc: PortalError, *, timeouts: int
    ) -> int:
        """Decide whether one query's failure ends the window.

        Returns the running count of consecutive timeouts. Setting
        ``batch.error`` is what tells the caller to stop; leaving it unset means
        "try the next query", which is worth doing only because the fan-out
        adapters cache their boards.
        """
        if isinstance(exc, PortalRateLimitedError | PortalBlockedError):
            # A refusal. Asking again in any form would be ignoring it.
            batch.error = exc
            return timeouts

        if not isinstance(exc, PortalTimeoutError):
            # A parse error or a broken adapter: the next query would fail the
            # same way, and the failure is worth surfacing rather than burying.
            batch.error = exc
            return timeouts

        timeouts += 1
        batch.lost_queries += 1
        self._lost_queries += 1
        if timeouts >= MAX_CONSECUTIVE_TIMEOUTS or self.budget_exhausted:
            batch.error = exc
            return timeouts

        batch.tolerated_error = exc
        logger.info(
            "portal_query_timed_out_continuing",
            portal=self._adapter.slug,
            window=batch.window.value,
            collected_so_far=len(batch.jobs),
            consecutive_timeouts=timeouts,
        )
        return timeouts

    @staticmethod
    def _settle(batch: WindowBatch) -> None:
        """Resolve a timeout that was tolerated but never recovered from.

        Called once the window's queries are done and its jobs are known.

        A tolerated timeout stops being a *failure* once some other query
        reached the portal and answered. That answer may well be "no such
        jobs": a portal that was read and holds nothing is not a portal that is
        down, and calling it failed is both untrue and expensive, because it
        discards the queries that did succeed. Measured on Lever for Financial
        Analyst, six of seven queries answered zero against 1,536 live
        postings and the leg was still reported as failed.

        What the timeout always costs is completeness, so the window is marked
        ``partial`` either way and the run still says the portal had more to
        give. The unanswered query is never counted as covered.

        With nothing answered there is no evidence at all, and the window
        really did fail - so it says so rather than reporting an empty result.
        """
        if batch.error is not None or batch.tolerated_error is None:
            return
        if batch.jobs or batch.answered:
            batch.partial = True
        else:
            batch.error = batch.tolerated_error

    def _deadline(self) -> Deadline | None:
        """How long the next adapter call may run, as the adapter sees it.

        The tighter of the call's own ceiling and what is left of the leg
        budget, less a reserve so a cooperating adapter returns under its own
        power rather than being cancelled. ``None`` when neither is bounded.
        """
        ceilings = []
        if self._query_timeout > 0:
            ceilings.append(self._query_timeout)
        if self._budget > 0:
            spent = time.monotonic() - self._started_at
            ceilings.append(max(0.0, self._budget - spent))

        if not ceilings:
            return None

        seconds = min(ceilings)
        reserve = min(
            ADAPTER_RETURN_RESERVE_SECONDS, seconds * ADAPTER_RETURN_RESERVE_FRACTION
        )
        return Deadline.in_seconds(seconds, reserve=reserve)

    async def _search(
        self, query: str, window: FreshnessWindow
    ) -> tuple[list[RawJob], ExecutedQuery, bool, bool]:
        """One adapter call, timed and bounded.

        A timeout is reported as a portal failure rather than an empty result:
        an empty result would look like "this portal has no matching jobs",
        which is a materially different and misleading claim. Whether that
        failure ends the leg is decided by the caller, which knows what has
        already been collected.

        The third element of the return is the adapter's own report that it
        stopped early - a clean partial, not a failure. The fourth is whether
        the adapter actually put the query to the portal at all, which is what
        separates "the portal holds none of these" from "I had no way to ask".
        """
        deadline = self._deadline()
        request = PortalSearchRequest(
            queries=(query,),
            window=window,
            limit=self._per_window_limit,
            deadline=deadline,
        )

        self._requests += 1
        started = time.monotonic()

        try:
            # The same deadline the adapter is handed, made visible to the
            # shared HTTP layer as well. An adapter checks between requests;
            # only the HTTP layer can stop one from being *started* that will
            # outlive the ceiling below, and being cancelled there is what
            # destroys an adapter's collected-but-unreturned listings.
            with use_deadline(deadline):
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
            partial=result.partial,
        )

        executed = ExecutedQuery(
            query_text=query,
            window=window,
            page=1,
            result_count=len(jobs),
            executed_at=datetime.now(UTC),
            duration_ms=duration_ms,
        )
        return jobs, executed, bool(result.partial), bool(result.consulted)

    def _admit(self, job: RawJob) -> bool:
        """True the first time this listing is seen in this run."""
        identity = _identity(job)
        if identity in self._yielded:
            return False
        self._yielded.add(identity)
        return True


def _reason_for(exc: PortalError) -> ExhaustionReason:
    """Which stopping condition a failure represents."""
    if isinstance(exc, PortalRateLimitedError):
        return ExhaustionReason.RATE_LIMITED
    if isinstance(exc, PortalTimeoutError):
        return ExhaustionReason.TIMEOUT
    return ExhaustionReason.ERROR


__all__ = [
    "ADAPTER_RETURN_RESERVE_SECONDS",
    "DEFAULT_PORTAL_BUDGET_SECONDS",
    "DEFAULT_QUERY_TIMEOUT_SECONDS",
    "MAX_CONSECUTIVE_TIMEOUTS",
    "DeepCollection",
    "ExecutedQuery",
    "PortalCollector",
    "WindowBatch",
]
