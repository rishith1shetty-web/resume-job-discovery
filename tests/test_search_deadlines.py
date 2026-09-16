"""What a portal keeps when it runs out of time.

The bug these cover, stated plainly: nearly every adapter fans out - over
employer boards, tenants, taxonomy pages or result pages - so a single
``search`` call is many requests. Ten of the eleven cannot filter by date
either, which means the collector loads the whole freshness ladder from one
pool built on the *first* window. Every request a portal makes in a run was
therefore made inside one pass, and a timeout anywhere in that pass discarded
the pass, the pool and the leg. A portal that had read nineteen boards and
stalled on the twentieth was recorded as "failed, 0 collected".

Two independent mechanisms fix it, and both are tested here:

* **Cooperative deadlines.** Each call carries how long it may run. An adapter
  that fans out checks before its next request and returns what it holds.
  Cancellation destroys a coroutine's locals; returning does not.
* **Partial preservation in the collector.** For whatever the deadline does not
  catch - a request already in flight, an adapter that ignores the deadline -
  the batch keeps the listings the earlier queries returned instead of dying
  with the exception.

The rule that decides between a partial result and a failure is the same one
the collector has always applied to an empty result: **a claim of "no matching
jobs" must be earned.** Nothing collected means failure, reported as such.
Something collected means the jobs are kept and the sweep is reported as cut
short. Neither is allowed to masquerade as the other.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
import pytest

from app.portals.base import (
    BasePortal,
    Deadline,
    PortalCapabilities,
    PortalSearchRequest,
    PortalSearchResult,
    RawJob,
    out_of_time,
)
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.portals.http import HttpPolicy, PortalHttpClient
from app.search.collection import (
    MAX_CONSECUTIVE_TIMEOUTS,
    PortalCollector,
)
from app.search.depth import ExhaustionReason, SearchDepth
from tests.fake_portals import make_raw_job

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

#: Six same-role variations, as the query planner produces (CLAUDE.md s3).
QUERIES = (
    "Business Analyst",
    "Senior Business Analyst",
    "Business Systems Analyst",
    "Product Analyst",
    "Process Analyst",
    "Requirements Analyst",
)


# --- A fan-out adapter, which is the shape that actually breaks -------------


class BoardPortal(BasePortal):
    """An adapter that reads a list of employer boards, one request each.

    Deliberately the shape of Greenhouse, Lever, Ashby, SmartRecruiters and
    Workday rather than a single-request portal: the whole problem is that one
    ``search`` here is many requests, so being cancelled loses work that a
    single-request adapter never has.

    ``honours_deadline`` exists so the collector's own preservation can be
    tested against an adapter that ignores the deadline entirely - which is
    what an adapter looks like before it is taught about one, and what a badly
    behaved future adapter would look like.
    """

    slug: ClassVar[str] = "boards"
    display_name: ClassVar[str] = "Board Portal"

    def __init__(
        self,
        options: dict[str, Any] | None = None,
        *,
        boards: int = 10,
        seconds_per_board: float = 0.1,
        jobs_per_board: int = 1,
        honours_deadline: bool = True,
        caches: bool = False,
        fail_with: Exception | None = None,
        fail_after_calls: int = 0,
    ) -> None:
        super().__init__(options)
        self._boards = [f"board-{index}" for index in range(boards)]
        self._caches = caches
        self._cache: set[str] = set()
        self._cost = seconds_per_board
        self._per_board = jobs_per_board
        self._honours = honours_deadline
        self._fail_with = fail_with
        self._fail_after = fail_after_calls
        #: Every board actually requested, across every call.
        self.read: list[str] = []
        #: Deadlines this adapter was handed, for asserting propagation.
        self.deadlines: list[Deadline | None] = []
        self.calls = 0

    @property
    def capabilities(self) -> PortalCapabilities:
        # False is the majority case: ten of the eleven shipped adapters
        # cannot filter by date, which is what puts the whole run's request
        # cost inside the first window.
        return PortalCapabilities(supports_date_filter=False)

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        self.deadlines.append(request.deadline)
        self.calls += 1

        if self._fail_with is not None and self.calls > self._fail_after:
            raise self._fail_with

        jobs: list[RawJob] = []
        truncated = False

        for board in self._boards:
            if self._caches and board in self._cache:
                # A cached board costs nothing, so it is never weighed against
                # the deadline - this is what lets a later query walk past the
                # point an earlier one stopped at.
                continue

            if self._honours and out_of_time(request):
                truncated = True
                break

            await asyncio.sleep(self._cost)
            self._cache.add(board)
            self.read.append(board)

            for offset in range(self._per_board):
                index = len(jobs)
                jobs.append(
                    make_raw_job(
                        portal=self.slug,
                        index=index,
                        external_id=f"{board}-{offset}",
                        # Spread across the ladder on purpose. A listing four
                        # days old belongs to no window until the walk reaches
                        # LAST_7D, so a leg that stops walking loses it - even
                        # though re-filtering the pool costs nothing.
                        age_days=0.1 + index * 2.0,
                        now=NOW,
                    )
                )

        return PortalSearchResult(
            jobs=jobs, window=request.window, partial=truncated
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        return job.description

    async def validate_url(self, url: str) -> bool:
        return True


#: A board that costs far less than the reserve, and far more boards than the
#: ceiling allows. This is the production proportion - one request is a small
#: fraction of a 90-second ceiling with a 5-second reserve - and it is what lets
#: the adapter return under its own power instead of being cancelled. A fake
#: whose single request eats a quarter of the ceiling would test the backstop,
#: not the cooperative path.
SLOW_BOARDS = 60
SLOW_SECONDS_PER_BOARD = 0.02
#: Comfortably more than one board's cost above the reserve. The event loop's
#: timer granularity is ~15ms on Windows, so a ceiling only narrowly above the
#: reserve would sometimes let the adapter overshoot into the hard timeout and
#: make these tests flaky about which mechanism caught it.
SLOW_CEILING = 1.0
#: Two calls' worth, so the leg ends on its budget rather than on six slow
#: queries. Keeps the suite fast without weakening what is asserted.
SLOW_BUDGET = 1.3

#: Scale for the tests that keep the real ``PortalHttpClient`` in the path.
#: Roomier than SLOW_CEILING because the adapter's margin for returning is the
#: reserve, and the reserve is a tenth of the ceiling - at 1.0s that is 100ms,
#: which is inside the noise of an event loop on Windows.
HTTP_CEILING = 1.5
HTTP_BUDGET = 2.0


def collector(adapter: BasePortal, **kwargs: Any) -> PortalCollector:
    kwargs.setdefault("queries", QUERIES)
    kwargs.setdefault("per_window_limit", 100)
    return PortalCollector(adapter, **kwargs)


async def collect(adapter: BasePortal, **kwargs: Any):  # noqa: ANN201
    depth = kwargs.pop("depth", SearchDepth())
    return await collector(adapter, **kwargs).collect(now=NOW, depth=depth)


# --- The Deadline value itself ---------------------------------------------


def test_a_deadline_with_time_left_permits_work() -> None:
    deadline = Deadline.in_seconds(60.0)

    assert not deadline.expired
    assert deadline.remaining == pytest.approx(60.0, abs=1.0)
    assert deadline.allows(10.0)


def test_a_deadline_reserves_time_for_the_adapter_to_return() -> None:
    """The reserve is the point: expiring *before* the hard timeout is what
    turns a cancellation into a cooperative return."""
    deadline = Deadline.in_seconds(10.0, reserve=4.0)

    assert deadline.remaining == pytest.approx(6.0, abs=1.0)
    assert not deadline.allows(8.0)


def test_an_expired_deadline_reports_no_remaining_time() -> None:
    deadline = Deadline.in_seconds(-5.0)

    assert deadline.expired
    assert deadline.remaining == 0.0
    assert not deadline.allows(0.0)


def test_an_adapter_with_no_deadline_is_never_out_of_time() -> None:
    """Adding the guard must not change how an adapter behaves for a caller
    that imposes no deadline - a direct call, a health check, a test."""
    from app.core.constants import FreshnessWindow

    request = PortalSearchRequest(
        queries=("Business Analyst",), window=FreshnessWindow.LAST_7D, limit=10
    )

    assert request.deadline is None
    assert not out_of_time(request)


# --- A fast portal that finishes normally ----------------------------------


async def test_a_fast_portal_is_untouched_by_any_of_this() -> None:
    """The regression case. A portal that finishes has no deadline behaviour
    to observe: every board is read, nothing is marked partial, and the leg is
    reported exhausted exactly as before."""
    adapter = BoardPortal(boards=4, seconds_per_board=0.0, jobs_per_board=2)

    collected = await collect(adapter)

    # Four boards, once - not once per query. The adapter is asked once per
    # query but reads each board on the first call and caches nothing here, so
    # this is the honest upper bound rather than a claim about caching.
    assert len(adapter.read) == 4 * len(QUERIES)
    assert len(collected.jobs) == 8
    assert collected.reason is ExhaustionReason.EXHAUSTED
    assert collected.is_complete
    assert not collected.is_partial
    assert collected.error is None
    assert collected.lost_queries == 0


async def test_a_fast_portal_still_walks_the_whole_freshness_ladder() -> None:
    """Nothing here may shorten the ladder for a portal that is keeping up."""
    from app.core.constants import FRESHNESS_ESCALATION

    adapter = BoardPortal(boards=2, seconds_per_board=0.0)

    batches = [
        batch
        async for batch in collector(adapter).windows(now=NOW)
    ]

    assert len(batches) == len(FRESHNESS_ESCALATION)
    assert not any(batch.cut_short for batch in batches)


# --- A multi-board portal that returns partial results on deadline ---------


async def test_a_board_portal_out_of_time_returns_the_boards_it_read() -> None:
    """The acceptance condition, at the adapter boundary.

    Ten boards at 0.1s each is a second of work against a 0.4s ceiling. The
    adapter must come back with the boards it managed, not with nothing.
    """
    adapter = BoardPortal(
        boards=SLOW_BOARDS, seconds_per_board=SLOW_SECONDS_PER_BOARD
    )

    collected = await collect(
        adapter,
        query_timeout_seconds=SLOW_CEILING,
        budget_seconds=SLOW_BUDGET,
    )

    assert 0 < len(adapter.read) < SLOW_BOARDS * len(QUERIES)
    assert collected.jobs, "the whole point: jobs survive the deadline"
    assert collected.error is None, "a deadline is not a failure"
    assert collected.reason is ExhaustionReason.TIMEOUT
    assert collected.is_partial
    assert not collected.is_complete


async def test_a_cut_short_portal_stops_spending_but_keeps_filtering() -> None:
    """Being cut short ends the *spending*, not the walk.

    The subtle half of this. A portal that cannot filter by date is fetched
    once into a pool and every later window re-filters that pool for free, so
    abandoning the ladder on a timeout throws away eligible listings at no
    saving whatever - a job four days old is admitted at LAST_7D and nowhere
    earlier. What must stop is requests, and nothing else.
    """
    from app.core.constants import FRESHNESS_ESCALATION

    adapter = BoardPortal(
        boards=SLOW_BOARDS, seconds_per_board=SLOW_SECONDS_PER_BOARD
    )

    batches = [
        batch
        async for batch in collector(
            adapter,
            query_timeout_seconds=SLOW_CEILING,
            budget_seconds=SLOW_BUDGET,
        ).windows(now=NOW)
    ]

    assert any(batch.cut_short for batch in batches)
    assert len(batches) == len(FRESHNESS_ESCALATION), "the free walk continues"

    # Not one request after the cut, however many windows followed: the pool is
    # loaded on the first window and every later window re-filters it.
    assert sum(1 for batch in batches if batch.fetched) == 1
    # And the later windows really did admit listings the first could not.
    assert sum(len(batch.jobs) for batch in batches[1:]) > 0


async def test_a_deadline_defers_a_board_rather_than_dropping_it() -> None:
    """Why truncating a board list is tolerable rather than a coverage loss.

    Every fan-out adapter caches what it reads for the life of the run, and the
    collector issues one call per same-role variation. So the query after a
    truncated one skips the boards already read - they cost nothing - and
    spends its own deadline on the boards the previous query never reached. The
    list is walked progressively rather than restarted, and the employers at
    the end of it are reached late rather than never.

    This is what makes the shipped board orders defensible without reordering
    them on evidence that does not exist: a board late in the list is deferred
    by a deadline, not excluded by one.
    """
    one_query = BoardPortal(
        boards=SLOW_BOARDS,
        seconds_per_board=SLOW_SECONDS_PER_BOARD,
        caches=True,
    )
    await collector(
        one_query,
        queries=("Business Analyst",),
        query_timeout_seconds=SLOW_CEILING,
        budget_seconds=SLOW_BUDGET,
    ).collect(now=NOW, depth=SearchDepth())

    every_query = BoardPortal(
        boards=SLOW_BOARDS,
        seconds_per_board=SLOW_SECONDS_PER_BOARD,
        caches=True,
    )
    await collector(
        every_query,
        query_timeout_seconds=SLOW_CEILING,
        budget_seconds=SLOW_BUDGET,
    ).collect(now=NOW, depth=SearchDepth())

    assert len(one_query.read) < SLOW_BOARDS, "the first query is truncated"
    assert len(every_query.read) > len(one_query.read), (
        "later queries must resume past the truncation point, not restart"
    )
    # No board is ever read twice, however many variations asked for it.
    assert len(every_query.read) == len(set(every_query.read))


async def test_a_cached_board_is_never_requested_twice() -> None:
    """Query overlap reduction, stated as a property.

    Six same-role variations against one board list is one request per board,
    not six. The adapters whose requests are query-*independent* - Greenhouse,
    Lever and Ashby fetch whole boards, Jobright and Hiring Cafe fetch taxonomy
    pages - all cache exactly this way. The ones that do not cache, SimplyHired,
    Scale.jobs, Google Careers and Dayforce, put the query in the URL, so their
    variations are genuinely different questions rather than repeated ones.
    """
    adapter = BoardPortal(boards=5, seconds_per_board=0.0, caches=True)

    await collect(adapter)

    assert adapter.calls == len(QUERIES), "asked once per variation"
    assert len(adapter.read) == 5, "but each board fetched exactly once"


async def test_an_exhausted_leg_budget_still_walks_the_free_windows() -> None:
    """The same rule, reached by the other road.

    A leg that spends its whole budget on the first window has still paid for a
    pool, and the windows that admit anything older than a day cost nothing to
    apply. Stopping on the budget was quietly restricting a slow portal to
    listings posted in the last twenty-four hours - not a timeout anyone would
    see in the logs, just a much smaller result set.
    """
    from app.core.constants import FRESHNESS_ESCALATION

    adapter = BoardPortal(
        boards=SLOW_BOARDS, seconds_per_board=SLOW_SECONDS_PER_BOARD
    )

    collected = await collector(
        adapter,
        query_timeout_seconds=SLOW_CEILING,
        budget_seconds=SLOW_BUDGET,
    ).collect(now=NOW, depth=SearchDepth())

    assert collected.deepest_window is FRESHNESS_ESCALATION[-1]
    assert collected.reason is ExhaustionReason.TIMEOUT
    assert collected.is_partial
    # Listings older than a day, which only the wider windows admit.
    assert any(
        (NOW - job.posted_at).days >= 1 for job in collected.jobs
    ), "the free windows really did contribute"


async def test_a_date_filtering_portal_does_stop_when_cut_short() -> None:
    """The mirror case: here a wider window *is* another request, so the walk
    ends rather than spending what was just ruled out."""
    from app.core.constants import FRESHNESS_ESCALATION

    class Filtered(BoardPortal):
        slug = "filtered"

        @property
        def capabilities(self) -> PortalCapabilities:
            return PortalCapabilities(supports_date_filter=True)

    adapter = Filtered(
        boards=SLOW_BOARDS, seconds_per_board=SLOW_SECONDS_PER_BOARD
    )

    batches = [
        batch
        async for batch in collector(
            adapter,
            query_timeout_seconds=SLOW_CEILING,
            budget_seconds=SLOW_BUDGET,
        ).windows(now=NOW)
    ]

    assert batches[-1].cut_short
    assert len(batches) < len(FRESHNESS_ESCALATION)


async def test_the_deadline_is_tightened_by_what_is_left_of_the_leg_budget(
) -> None:
    """A call may not be handed 90 seconds when the leg has 5 seconds left."""
    adapter = BoardPortal(boards=1, seconds_per_board=0.0)

    await collect(adapter, query_timeout_seconds=90.0, budget_seconds=1.0)

    first = adapter.deadlines[0]
    assert first is not None
    # Bounded by the budget, not by the 90s call ceiling.
    assert first.remaining <= 1.0


async def test_an_adapter_that_ignores_the_deadline_still_keeps_its_jobs(
) -> None:
    """The collector's own half of the fix.

    This adapter never checks the deadline, so its fourth query is cancelled
    outright and everything that call had collected is genuinely lost. What
    must survive is the pool the first three queries built - and before this
    change, it did not.
    """


    class AnswersThenHangs(BasePortal):
        """Answers the first query, then stops responding. Never checks the
        deadline, so the hard timeout is the only thing that can stop it."""

        slug: ClassVar[str] = "hangs"
        display_name: ClassVar[str] = "Hangs"

        def __init__(self, options: dict[str, Any] | None = None) -> None:
            super().__init__(options)
            self.calls = 0

        @property
        def capabilities(self) -> PortalCapabilities:
            return PortalCapabilities(supports_date_filter=False)

        async def search(self, request: PortalSearchRequest):  # noqa: ANN201
            self.calls += 1
            if self.calls > 1:
                await asyncio.sleep(30)
            return PortalSearchResult(
                jobs=[
                    make_raw_job(
                        portal=self.slug,
                        index=index,
                        external_id=f"hangs-{index}",
                        age_days=0.1,
                        now=NOW,
                    )
                    for index in range(3)
                ],
                window=request.window,
            )

        async def fetch_description(self, job: RawJob) -> str | None:
            return job.description

        async def validate_url(self, url: str) -> bool:
            return True

    adapter = AnswersThenHangs()

    collected = await collect(adapter, query_timeout_seconds=0.05)

    assert len(collected.jobs) == 3, "the first query's listings survive"
    assert collected.error is None
    assert collected.reason is ExhaustionReason.TIMEOUT
    assert collected.is_partial


# --- A portal that finds zero jobs before timing out -----------------------


async def test_running_out_of_time_having_found_nothing_is_not_completion(
) -> None:
    """"Complete" would be the wrong word.

    Nothing was collected, but nothing was refused either - the portal simply
    was not reached. Reporting that as a clean sweep would claim this employer
    set has no Business Analyst vacancies, which was never established.
    """
    adapter = BoardPortal(
        boards=SLOW_BOARDS,
        seconds_per_board=SLOW_SECONDS_PER_BOARD,
        jobs_per_board=0,
    )

    collected = await collect(
        adapter,
        query_timeout_seconds=SLOW_CEILING,
        budget_seconds=SLOW_BUDGET,
    )

    assert collected.jobs == []
    assert collected.error is None, "not a failure: nothing broke"
    assert collected.reason is ExhaustionReason.TIMEOUT
    assert collected.is_partial
    assert not collected.is_complete


async def test_a_portal_that_genuinely_has_nothing_is_complete() -> None:
    """The contrast that gives the test above its meaning."""
    adapter = BoardPortal(boards=3, seconds_per_board=0.0, jobs_per_board=0)

    collected = await collect(adapter)

    assert collected.jobs == []
    assert collected.reason is ExhaustionReason.EXHAUSTED
    assert collected.is_complete
    assert not collected.is_partial


# --- A real hard failure ---------------------------------------------------


async def test_a_parse_failure_with_nothing_collected_still_fails_the_leg(
) -> None:
    """Unchanged, and it must stay unchanged. An empty result would read as
    "this portal has no matching jobs", which is a different claim."""
    adapter = BoardPortal(
        boards=5, seconds_per_board=0.0, fail_with=PortalParseError(
            "shape changed", portal="boards"
        )
    )

    collected = await collect(adapter)

    assert collected.jobs == []
    assert isinstance(collected.error, PortalParseError)
    assert collected.reason is ExhaustionReason.ERROR
    assert not collected.is_partial


async def test_a_parse_failure_is_never_retried_across_queries() -> None:
    """The next query would fail identically; spending the budget to prove it
    would be pointless, and burying the error would be worse."""
    adapter = BoardPortal(
        boards=5,
        seconds_per_board=0.0,
        fail_with=PortalParseError("shape changed", portal="boards"),
    )

    await collect(adapter)

    assert len(adapter.deadlines) == 1, "one attempt, not six"


async def test_a_failure_after_collecting_keeps_what_was_collected() -> None:
    """Losing forty good listings because the forty-first request broke would
    be the wrong trade."""
    adapter = BoardPortal(
        boards=5,
        seconds_per_board=0.0,
        fail_with=PortalError("boom", portal="boards"),
        fail_after_calls=1,
    )

    collected = await collect(adapter)

    assert collected.jobs, "the first query's listings survive the second's failure"
    assert collected.error is None
    assert collected.reason is ExhaustionReason.ERROR
    assert collected.is_partial


# --- A robots / access failure --------------------------------------------


async def test_a_refusal_is_respected_immediately_and_never_retried() -> None:
    """A portal that says no has said no.

    The timeout tolerance below deliberately does not apply here: retrying a
    refusal in any form would be ignoring what the portal just said, whatever
    the remaining budget allows.
    """
    adapter = BoardPortal(
        boards=5,
        seconds_per_board=0.0,
        fail_with=PortalBlockedError("robots.txt declines", portal="boards"),
    )

    collected = await collect(adapter)

    assert len(adapter.deadlines) == 1, "asked once, refused, not asked again"
    assert isinstance(collected.error, PortalBlockedError)
    assert collected.reason is ExhaustionReason.ERROR


async def test_a_refusal_after_collecting_still_stops_at_once() -> None:
    """The jobs already read are kept - they were served willingly - but not
    one further request is made."""
    adapter = BoardPortal(
        boards=5,
        seconds_per_board=0.0,
        fail_with=PortalBlockedError("blocked", portal="boards"),
        fail_after_calls=1,
    )

    collected = await collect(adapter)

    assert collected.jobs, "served willingly before the refusal, so kept"
    assert adapter.calls == 2, "one answer, one refusal, then nothing"
    assert collected.error is None
    assert collected.is_partial


async def test_rate_limiting_is_a_refusal_not_a_timeout() -> None:
    adapter = BoardPortal(
        boards=5,
        seconds_per_board=0.0,
        fail_with=PortalRateLimitedError("slow down", portal="boards"),
    )

    collected = await collect(adapter)

    assert len(adapter.deadlines) == 1
    assert collected.reason is ExhaustionReason.RATE_LIMITED


# --- Tolerating a timeout, but only so far --------------------------------


class FlakyPortal(BasePortal):
    """Times out for the first ``timeouts`` calls, then answers."""

    slug: ClassVar[str] = "flaky"
    display_name: ClassVar[str] = "Flaky Portal"

    def __init__(
        self, options: dict[str, Any] | None = None, *, timeouts: int = 1
    ) -> None:
        super().__init__(options)
        self._budget = timeouts
        self.calls = 0

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(supports_date_filter=False)

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        self.calls += 1
        if self.calls <= self._budget:
            raise PortalTimeoutError("did not respond", portal=self.slug)
        return PortalSearchResult(
            jobs=[
                make_raw_job(
                    portal=self.slug,
                    index=self.calls,
                    external_id=f"flaky-{self.calls}",
                    age_days=1.0,
                    now=NOW,
                )
            ],
            window=request.window,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        return job.description

    async def validate_url(self, url: str) -> bool:
        return True


async def test_one_timeout_does_not_abandon_the_remaining_queries() -> None:
    """The portals that fan out cache their boards, so the query after a
    timed-out one is usually served from that cache. Abandoning the leg would
    throw away work already paid for."""
    adapter = FlakyPortal(timeouts=1)

    collected = await collect(adapter)

    assert adapter.calls == len(QUERIES), "carried on through all six"
    assert collected.jobs
    assert collected.lost_queries == 1
    assert collected.error is None


async def test_consecutive_timeouts_are_capped() -> None:
    """Past the cap the evidence says the portal is not answering, and the
    budget is better left unspent than burned proving it again."""
    adapter = FlakyPortal(timeouts=99)

    collected = await collect(adapter)

    assert adapter.calls == MAX_CONSECUTIVE_TIMEOUTS
    assert isinstance(collected.error, PortalTimeoutError)
    assert collected.reason is ExhaustionReason.TIMEOUT


async def test_a_lone_timeout_is_not_quietly_forgiven() -> None:
    """One query, one timeout, no recovery: that leg failed.

    The tolerance must not turn a portal that never answered into a portal
    that answered with nothing.
    """
    adapter = FlakyPortal(timeouts=99)

    collected = await collect(adapter, queries=("Business Analyst",))

    assert adapter.calls == 1
    assert isinstance(collected.error, PortalTimeoutError)


class _TimesOutThenEmpty(BasePortal):
    """Times out once, then answers every later query with nothing.

    ``consulted`` is what the answer *means*: True says the portal was read and
    holds no such jobs, False says the adapter had no way to ask. The two look
    identical from the outside - both are an empty list - and they are the two
    cases this pair of tests keeps apart.
    """

    slug: ClassVar[str] = "empty"
    display_name: ClassVar[str] = "Empty"
    consulted: ClassVar[bool] = True

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.calls = 0

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(supports_date_filter=False)

    async def search(self, request: PortalSearchRequest):  # noqa: ANN201
        self.calls += 1
        if self.calls == 1:
            raise PortalTimeoutError("no answer", portal=self.slug)
        return PortalSearchResult(
            jobs=[], window=request.window, consulted=type(self).consulted
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        return None

    async def validate_url(self, url: str) -> bool:
        return True


async def test_a_definitive_zero_is_not_a_failure_even_after_a_lost_query() -> None:
    """A portal that was read and holds nothing is not a portal that is down.

    Measured live on Lever for Financial Analyst: six of seven queries answered
    zero against 1,536 real postings and one timed out, and the leg was
    reported as *failed* - which said the portal was unreachable when it had in
    fact been read end to end. The answer is still incomplete, so the leg stays
    partial and the lost query is still counted; what it is not is a failure.
    """

    class Reached(_TimesOutThenEmpty):
        consulted: ClassVar[bool] = True

    collected = await collect(Reached())

    assert collected.jobs == []
    assert collected.error is None
    assert collected.reason is ExhaustionReason.TIMEOUT
    # Incomplete, and saying so: the timed-out query was never covered.
    assert collected.is_partial is True
    assert collected.lost_queries == 1


async def test_an_unasked_query_does_not_forgive_an_unanswered_one() -> None:
    """Several adapters legitimately return nothing for a query they cannot
    express - Jobright and Hiring Cafe do it whenever a variation matches no
    taxonomy slug, without making a single request. Treating that as proof the
    timed-out query would also have been empty would let the leg report "no
    matching jobs" for a question the portal was never asked.
    """

    class NeverAsked(_TimesOutThenEmpty):
        consulted: ClassVar[bool] = False

    collected = await collect(NeverAsked())

    assert collected.jobs == []
    assert isinstance(collected.error, PortalTimeoutError)
    assert collected.reason is ExhaustionReason.TIMEOUT
    assert collected.lost_queries == 1


# --- A portal with its own timeout override -------------------------------


async def test_a_portals_own_query_ceiling_is_what_reaches_its_adapter() -> None:
    """Workday runs on 150s and SmartRecruiters on the shared 90s. Whatever a
    portal is configured with is what its adapter must be told it has - a
    deadline derived from a global default would silently undo the override.
    """
    depth = SearchDepth.from_options(
        {"query_timeout_seconds": 150.0, "max_runtime_seconds": 300.0}
    )
    adapter = BoardPortal(boards=1, seconds_per_board=0.0)

    await collect(
        adapter,
        depth=depth,
        query_timeout_seconds=depth.query_timeout_seconds,
        budget_seconds=depth.max_runtime_seconds,
    )

    deadline = adapter.deadlines[0]
    assert deadline is not None
    # 150s less the capped reserve, not the 90s shared ceiling.
    assert deadline.remaining == pytest.approx(145.0, abs=2.0)


async def test_the_shared_ceiling_is_unchanged_for_everyone_else() -> None:
    from app.search.depth import (
        DEFAULT_MAX_RUNTIME_SECONDS,
        DEFAULT_QUERY_TIMEOUT_SECONDS,
    )

    assert DEFAULT_QUERY_TIMEOUT_SECONDS == 90.0
    assert DEFAULT_MAX_RUNTIME_SECONDS == 150.0

    adapter = BoardPortal(boards=1, seconds_per_board=0.0)
    await collect(
        adapter,
        query_timeout_seconds=DEFAULT_QUERY_TIMEOUT_SECONDS,
        budget_seconds=DEFAULT_MAX_RUNTIME_SECONDS,
    )

    deadline = adapter.deadlines[0]
    assert deadline is not None
    assert deadline.remaining == pytest.approx(85.0, abs=2.0)


async def test_an_unbounded_leg_budget_leaves_the_call_ceiling_in_charge() -> None:
    """A budget of zero is the existing "no limit" convention for the leg. The
    per-call ceiling still applies, and it is what the deadline is built from -
    a leg with no budget must not hand its adapter an expired deadline."""
    adapter = BoardPortal(boards=2, seconds_per_board=0.0)

    await collect(adapter, query_timeout_seconds=90.0, budget_seconds=0.0)

    deadline = adapter.deadlines[0]
    assert deadline is not None
    assert deadline.remaining == pytest.approx(85.0, abs=2.0)
    assert len(adapter.read) == 2 * len(QUERIES), "nothing was cut short"


# --- Career Pages, which shares this execution path -----------------------


async def test_career_page_discovery_keeps_what_it_collected() -> None:
    """Career Pages runs through the same collector when it is selected on its
    own, so it inherits the same fix: an adapter that runs out of time still
    contributes the employers it did discover."""
    from app.careers.source import collect_discovery_listings

    quick = BoardPortal(boards=2, seconds_per_board=0.0)
    slow = BoardPortal(
        boards=SLOW_BOARDS, seconds_per_board=SLOW_SECONDS_PER_BOARD
    )
    slow.slug = "slow-boards"  # type: ignore[misc]

    listings = await collect_discovery_listings(
        [quick, slow], queries=QUERIES, now=NOW, budget_seconds=SLOW_BUDGET
    )

    assert listings, "a slow portal must not empty the discovery pass"
    assert any(job.source == "boards" for job in listings)


async def test_career_page_discovery_isolates_a_failing_adapter() -> None:
    """Unchanged behaviour, pinned because the path now has more ways to end."""
    from app.careers.source import collect_discovery_listings

    healthy = BoardPortal(boards=2, seconds_per_board=0.0)
    broken = BoardPortal(
        boards=2,
        seconds_per_board=0.0,
        fail_with=PortalParseError("shape", portal="boards"),
    )
    broken.slug = "broken-boards"  # type: ignore[misc]

    listings = await collect_discovery_listings(
        [healthy, broken], queries=QUERIES, now=NOW
    )

    assert listings
    assert all(job.source == "boards" for job in listings)


# --- Depth limits are not deadlines ---------------------------------------


async def test_reaching_the_job_ceiling_is_the_limit_working_not_a_timeout(
) -> None:
    """``max_jobs`` stopping a leg is configuration doing its job. Calling that
    a partial result would report a limit as a fault."""
    adapter = BoardPortal(boards=10, seconds_per_board=0.0, jobs_per_board=5)

    collected = await collect(adapter, depth=SearchDepth(max_jobs=6))

    assert len(collected.jobs) == 6
    assert collected.reason is ExhaustionReason.MAX_JOBS
    assert not collected.is_partial
    assert collected.error is None


def test_the_three_outcomes_are_distinct_in_telemetry() -> None:
    """complete / partial_timeout / failed - the distinction the run report
    exists to make."""
    import uuid

    from app.core.constants import PortalRunStatus
    from app.services.search_orchestrator import PortalLegOutcome

    def leg(**kwargs: Any) -> PortalLegOutcome:
        return PortalLegOutcome(
            portal_id=uuid.uuid4(),
            portal_slug="p",
            portal_display_name="P",
            index=0,
            status=kwargs.pop("status", PortalRunStatus.COMPLETED),
            target_quota=0,
            **kwargs,
        )

    assert leg().completion == "complete"
    assert leg(partial=True).completion == "partial_timeout"
    assert leg(status=PortalRunStatus.FAILED).completion == "failed"
    # A failure outranks partiality: there is nothing to be partial about.
    assert (
        leg(status=PortalRunStatus.FAILED, partial=True).completion == "failed"
    )


# --- The shipped adapters honour the contract -----------------------------


@pytest.mark.parametrize(
    "module_name, class_name",
    [
        ("ashby", "AshbyPortal"),
        ("dayforce", "DayforcePortal"),
        ("google_careers", "GoogleCareersPortal"),
        ("greenhouse", "GreenhousePortal"),
        ("hiringcafe", "HiringCafePortal"),
        ("jobright", "JobrightPortal"),
        ("lever", "LeverPortal"),
        ("scalejobs", "ScaleJobsPortal"),
        ("simplyhired", "SimplyHiredPortal"),
        ("smartrecruiters", "SmartRecruitersPortal"),
        ("workday", "WorkdayPortal"),
    ],
)
async def test_every_shipped_adapter_stops_on_an_expired_deadline(
    module_name: str, class_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No network, and none may be reached.

    An adapter handed an already-expired deadline must return without making a
    single request. The assertion is the *absence* of a request: every transport
    method is replaced with one that raises, so a call fails the test loudly
    rather than reaching a real portal.

    This is the test that keeps the guard honest for adapters whose first act
    is a request of their own - Dayforce opens a session, Jobright and Hiring
    Cafe load a taxonomy - where a guard placed only inside the board loop
    would have looked right and still spent a request.
    """
    import importlib

    from app.core.constants import FreshnessWindow
    from app.portals.http import PortalHttpClient

    module = importlib.import_module(f"app.portals.adapters.{module_name}")
    adapter = getattr(module, class_name)()

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            f"{module_name} made a request with an expired deadline"
        )

    for name in (
        "get_text",
        "get_json",
        "post_json",
        "get_optional_text",
        "head_ok",
    ):
        if hasattr(PortalHttpClient, name):
            monkeypatch.setattr(PortalHttpClient, name, refuse)

    result = await adapter.search(
        PortalSearchRequest(
            queries=("Business Analyst",),
            window=FreshnessWindow.LAST_7D,
            limit=25,
            deadline=Deadline.in_seconds(-1.0),
        )
    )

    assert result.jobs == []
    assert result.partial is True


def test_no_adapter_was_left_without_the_guard() -> None:
    """A new adapter that fans out and forgets this is the way the bug comes
    back, so the absence is asserted rather than trusted."""
    from pathlib import Path

    from app.portals.registry import registry

    import app.portals.adapters  # noqa: F401

    root = Path("app/portals/adapters")
    missing = [
        path.stem
        for path in sorted(root.glob("*.py"))
        if path.stem != "__init__"
        and "out_of_time(request)" not in path.read_text(encoding="utf-8")
    ]

    assert missing == [], f"adapters with no deadline guard: {missing}"
    assert len(registry.slugs) == 11


# --- The HTTP layer stops it before wait_for has to -------------------------


class HttpBoardPortal(BasePortal):
    """A fan-out adapter that reaches the portal through the real HTTP stack.

    The other fakes in this file short-circuit the transport, which is right
    for testing the collector but leaves the actual failure mode uncovered: the
    listings were lost inside ``PortalHttpClient``, on a request that started
    just inside the deadline and then ran on for its own full timeout. So this
    one keeps ``PortalHttpClient`` in the path and stubs only ``httpx``.
    """

    slug: ClassVar[str] = "httpboards"
    display_name: ClassVar[str] = "Http Boards"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug,
            policy=HttpPolicy(
                # Far longer than the leg: the point is that the clamp, not the
                # policy, is what ends the request.
                timeout_seconds=30.0,
                max_retries=0,
                retry_backoff_seconds=0.0,
                rate_limit_per_minute=0,
            ),
        )
        self.boards_read = 0

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(supports_date_filter=False)

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        jobs: list[RawJob] = []
        truncated = False

        for index in range(SLOW_BOARDS):
            if out_of_time(request):
                truncated = True
                break
            try:
                await self._http.get_text(f"https://example.invalid/board/{index}")
            except PortalError:
                # One board failing costs that board, exactly as every shipped
                # fan-out adapter treats it.
                truncated = True
                break
            self.boards_read += 1
            jobs.append(
                make_raw_job(
                    portal=self.slug,
                    index=index,
                    external_id=f"{self.slug}-{index}",
                    age_days=0.1,
                    now=NOW,
                )
            )

        return PortalSearchResult(jobs=jobs, window=request.window, partial=truncated)

    async def fetch_description(self, job: RawJob) -> str | None:
        return None

    async def validate_url(self, url: str) -> bool:
        return True


@pytest.fixture
def stalling_transport(monkeypatch: pytest.MonkeyPatch):
    """Stub httpx so that boards from ``stall_from`` onwards never answer.

    The stub enforces the client's configured timeout itself, because that is
    what the real transport does and it is the whole subject here: patching
    ``AsyncClient.get`` alone removes the timeout machinery, and a test built
    on that would pass whatever the production code did.
    """

    def _apply(stall_from: int):
        timeouts: list[float] = []

        def _init(self, *args, **kwargs):  # noqa: ANN001
            timeout = kwargs.get("timeout")
            seconds = timeout.read if isinstance(timeout, httpx.Timeout) else timeout
            timeouts.append(seconds)
            object.__setattr__(self, "_stub_timeout", seconds)

        async def _aenter(self):  # noqa: ANN001
            return self

        async def _aexit(self, *exc):  # noqa: ANN001
            return False

        async def _get(self, url, params=None):  # noqa: ANN001
            index = int(url.rsplit("/", 1)[-1])
            if index >= stall_from:
                try:
                    await asyncio.wait_for(
                        asyncio.sleep(30.0), timeout=self._stub_timeout
                    )
                except TimeoutError as exc:
                    raise httpx.ReadTimeout(
                        "stalled", request=httpx.Request("GET", url)
                    ) from exc
            return httpx.Response(
                status_code=200, text="ok", request=httpx.Request("GET", url)
            )

        # The real constructor is replaced rather than wrapped. Building an
        # httpx client costs 450-580ms here - every time, not just the first,
        # because `_send` builds one per attempt - which would dwarf the
        # sub-second deadlines these tests work in and make them measure SSL
        # setup instead of the clamp. Nothing real was left in the path anyway
        # once `get` is stubbed.
        monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
        monkeypatch.setattr(httpx.AsyncClient, "__aenter__", _aenter)
        monkeypatch.setattr(httpx.AsyncClient, "__aexit__", _aexit)
        monkeypatch.setattr(httpx.AsyncClient, "get", _get)
        return timeouts

    return _apply


async def test_the_http_layer_stops_a_sweep_before_wait_for_has_to(
    stalling_transport,
) -> None:
    """The regression this whole fix exists for.

    The very first board never answers. Before the clamp it ran for its own
    thirty seconds, blew through the one-second query ceiling and was cancelled
    by ``asyncio.wait_for`` - which destroys the adapter's locals. Now the
    request is cut to what is left of the deadline, the adapter gets a
    ``PortalError`` it already knows how to handle, and it returns.
    """
    timeouts = stalling_transport(0)

    adapter = HttpBoardPortal()
    collected = await collect(
        adapter,
        queries=("one",),
        query_timeout_seconds=HTTP_CEILING,
        budget_seconds=HTTP_BUDGET,
    )

    # Clamped down from the policy's thirty seconds to what the leg had left.
    assert timeouts and max(timeouts) < HTTP_CEILING
    # Cooperative: a hard `wait_for` kill leaves the collector holding a
    # PortalTimeoutError and no result at all.
    assert collected.error is None
    assert collected.reason is ExhaustionReason.TIMEOUT
    assert collected.is_partial is True


async def test_listings_read_before_the_deadline_survive_a_stalled_request(
    stalling_transport,
) -> None:
    """The acceptance condition in one test: a portal that reaches its deadline
    *after* finding useful jobs still returns them.
    """
    stalling_transport(3)

    adapter = HttpBoardPortal()
    collected = await collect(
        adapter,
        queries=("one",),
        query_timeout_seconds=HTTP_CEILING,
        budget_seconds=HTTP_BUDGET,
    )

    assert adapter.boards_read == 3
    # The three boards that answered are returned, not discarded.
    assert len(collected.jobs) == 3
    assert collected.error is None
    assert collected.is_partial is True
