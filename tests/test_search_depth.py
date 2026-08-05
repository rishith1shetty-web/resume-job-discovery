"""Deep unified search: how far a portal goes, and why it stops.

Phase 12. These cover the collection layer directly - the depth configuration,
the stopping conditions and the exhaustion reporting - plus the global Top N
selection that replaced per-portal quotas. The orchestration-level behaviour
against the database lives in ``test_search_orchestration.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import FRESHNESS_ESCALATION, FreshnessWindow
from app.matching.roles import RoleVerdict
from app.portals.base import (
    BasePortal,
    PortalCapabilities,
    PortalSearchRequest,
    PortalSearchResult,
    RawJob,
)
from app.portals.exceptions import (
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.search.allocation import Candidate, order_candidates, select_global_top
from app.search.collection import PortalCollector
from app.search.depth import (
    DEFAULT_MAX_JOBS,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_REQUESTS,
    ExhaustionReason,
    SearchDepth,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
QUERIES = ("Financial Analyst", "FP&A Analyst")


def raw(index: int, *, age_days: float = 0.1) -> RawJob:
    return RawJob(
        external_id=f"job-{index}",
        source="deep",
        title="Financial Analyst",
        company="Acme",
        location="Austin, TX",
        description="x" * 400,
        source_url=f"https://jobs.example.com/{index}",
        application_url=None,
        posted_at=NOW - timedelta(days=age_days),
        employment_type="Full-time",
        raw_payload={},
    )


class DeepPortal(BasePortal):
    """A portal with a configurable amount of inventory, counting its calls."""

    slug = "deep"
    display_name = "Deep"

    def __init__(self, options=None, *, jobs=None, date_filter=False, fail=None,
                 fail_after=0) -> None:
        super().__init__(options)
        self._jobs = jobs if jobs is not None else [raw(i) for i in range(10)]
        self._date_filter = date_filter
        self._fail = fail
        self._fail_after = fail_after
        self.calls: list[PortalSearchRequest] = []

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(supports_date_filter=self._date_filter)

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        self.calls.append(request)
        if self._fail is not None and len(self.calls) > self._fail_after:
            raise self._fail
        return PortalSearchResult(
            jobs=list(self._jobs[: request.limit]),
            window=request.window,
            window_applied_by_portal=self._date_filter,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        return job.description

    async def validate_url(self, url: str) -> bool:
        return True


def collector(adapter: BasePortal, *, per_window: int = 100, budget: float = 60.0):
    return PortalCollector(
        adapter,
        queries=QUERIES,
        per_window_limit=per_window,
        query_timeout_seconds=5.0,
        budget_seconds=budget,
    )


# --- Depth configuration ----------------------------------------------------


def test_the_defaults_are_finite() -> None:
    """"Until exhausted" must still be bounded."""
    depth = SearchDepth()

    assert depth.max_pages > 0
    assert depth.max_requests > 0
    assert depth.max_runtime_seconds > 0
    assert depth.max_jobs > 0


def test_depth_is_read_from_portal_configuration() -> None:
    depth = SearchDepth.from_options(
        {
            "max_pages": 3,
            "max_requests": 11,
            "max_runtime_seconds": 42.5,
            "max_jobs": 77,
            "per_request_limit": 25,
        }
    )

    assert depth.max_pages == 3
    assert depth.max_requests == 11
    assert depth.max_runtime_seconds == 42.5
    assert depth.max_jobs == 77
    assert depth.per_request_limit == 25


@pytest.mark.parametrize(
    "options",
    [
        {},
        None,
        {"max_pages": 0},
        {"max_pages": -4},
        {"max_pages": "lots"},
        {"max_pages": True},
        {"max_pages": None},
        {"unknown_key": 5},
    ],
)
def test_a_bad_or_missing_setting_falls_back_to_the_default(options) -> None:
    """A misconfigured row must not create an unbounded search."""
    depth = SearchDepth.from_options(options)

    assert depth.max_pages == DEFAULT_MAX_PAGES
    assert depth.max_requests == DEFAULT_MAX_REQUESTS
    assert depth.max_jobs == DEFAULT_MAX_JOBS


# --- Stopping conditions ----------------------------------------------------


async def test_a_portal_that_runs_out_reports_exhausted() -> None:
    """The goal state: nothing was left behind."""
    adapter = DeepPortal(jobs=[raw(i) for i in range(5)])

    collected = await collector(adapter).collect(now=NOW, depth=SearchDepth())

    assert collected.reason is ExhaustionReason.EXHAUSTED
    assert collected.is_complete
    assert collected.deepest_window is FreshnessWindow.OLDER_ACTIVE


async def test_collection_walks_the_whole_ladder_by_default() -> None:
    """No quota stops it part way any more."""
    adapter = DeepPortal(jobs=[raw(i) for i in range(50)], date_filter=True)

    collected = await collector(adapter).collect(now=NOW, depth=SearchDepth())

    windows = {call.window for call in adapter.calls}
    assert windows == set(FRESHNESS_ESCALATION)
    assert collected.reason is ExhaustionReason.EXHAUSTED


async def test_the_page_limit_stops_a_deeper_ladder() -> None:
    adapter = DeepPortal(jobs=[raw(i) for i in range(50)], date_filter=True)

    collected = await collector(adapter).collect(
        now=NOW, depth=SearchDepth(max_pages=2)
    )

    assert collected.reason is ExhaustionReason.MAX_PAGES
    assert not collected.is_complete
    assert collected.pages == 2


async def test_the_request_limit_stops_collection() -> None:
    adapter = DeepPortal(jobs=[raw(i) for i in range(50)], date_filter=True)

    collected = await collector(adapter).collect(
        now=NOW, depth=SearchDepth(max_requests=3)
    )

    assert collected.reason is ExhaustionReason.MAX_REQUESTS
    assert collected.requests >= 3
    # Never wildly past the budget: it is checked after every window.
    assert collected.requests <= 3 + len(QUERIES)


async def test_the_job_limit_stops_collection() -> None:
    adapter = DeepPortal(jobs=[raw(i) for i in range(80)])

    collected = await collector(adapter).collect(
        now=NOW, depth=SearchDepth(max_jobs=25)
    )

    assert collected.reason is ExhaustionReason.MAX_JOBS
    assert len(collected.jobs) == 25


async def test_the_runtime_budget_stops_collection() -> None:
    """A slow portal is cut off rather than holding the request open."""

    class SlowPortal(DeepPortal):
        async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
            await asyncio.sleep(0.05)
            return await super().search(request)

    adapter = SlowPortal(jobs=[raw(i) for i in range(5)], date_filter=True)

    collected = await PortalCollector(
        adapter,
        queries=QUERIES,
        per_window_limit=100,
        query_timeout_seconds=5.0,
        budget_seconds=0.06,
    ).collect(now=NOW, depth=SearchDepth(max_runtime_seconds=0.06))

    assert collected.reason is ExhaustionReason.TIMEOUT
    assert not collected.is_complete


async def test_a_portal_that_returns_nothing_is_not_an_error() -> None:
    adapter = DeepPortal(jobs=[])

    collected = await collector(adapter).collect(now=NOW, depth=SearchDepth())

    assert collected.jobs == []
    assert collected.error is None
    assert collected.reason is ExhaustionReason.EXHAUSTED


# --- Failures ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (PortalRateLimitedError("slow down", portal="deep"),
         ExhaustionReason.RATE_LIMITED),
        (PortalTimeoutError("too slow", portal="deep"), ExhaustionReason.TIMEOUT),
        (PortalParseError("bad shape", portal="deep"), ExhaustionReason.ERROR),
    ],
)
async def test_a_failure_partway_keeps_what_was_already_collected(
    error: PortalError, reason: ExhaustionReason
) -> None:
    """Losing forty good listings to the forty-first request is the wrong trade."""
    adapter = DeepPortal(
        jobs=[raw(i) for i in range(10)], date_filter=True, fail=error, fail_after=2
    )

    collected = await collector(adapter).collect(now=NOW, depth=SearchDepth())

    assert collected.jobs, "a partial result was discarded"
    assert collected.reason is reason
    assert collected.error is None, "a partial result must not fail the leg"
    assert not collected.is_complete


@pytest.mark.parametrize(
    "error",
    [
        PortalRateLimitedError("slow down", portal="deep"),
        PortalTimeoutError("too slow", portal="deep"),
        PortalParseError("bad shape", portal="deep"),
    ],
)
async def test_a_failure_before_anything_was_collected_fails_the_leg(
    error: PortalError,
) -> None:
    adapter = DeepPortal(fail=error, fail_after=0)

    collected = await collector(adapter).collect(now=NOW, depth=SearchDepth())

    assert collected.jobs == []
    assert collected.error is error


# --- Independence -----------------------------------------------------------


async def test_two_portals_collect_without_reference_to_each_other() -> None:
    """The heart of the phase: depth is a property of the portal alone."""
    rich = DeepPortal(jobs=[raw(i) for i in range(40)])
    thin = DeepPortal(jobs=[raw(i) for i in range(2)])

    rich_result, thin_result = await asyncio.gather(
        collector(rich).collect(now=NOW, depth=SearchDepth()),
        collector(thin).collect(now=NOW, depth=SearchDepth()),
    )

    assert len(rich_result.jobs) == 40
    assert len(thin_result.jobs) == 2
    assert rich_result.is_complete and thin_result.is_complete


async def test_a_listing_is_collected_once_however_deep_the_search() -> None:
    adapter = DeepPortal(jobs=[raw(i) for i in range(30)], date_filter=True)

    collected = await collector(adapter).collect(now=NOW, depth=SearchDepth())

    identities = [job.external_id for job in collected.jobs]
    assert len(identities) == len(set(identities))


# --- Global Top N -----------------------------------------------------------


def candidate(score: float, *, portal: int, key: str) -> Candidate:
    return Candidate(
        job_id=uuid.uuid5(uuid.NAMESPACE_URL, key),
        portal_index=portal,
        final_score=score,
        posted_at=NOW,
        role_verdict=RoleVerdict.EXACT,
        skill_coverage=1.0,
        canonical_key=key,
    )


def test_the_top_n_is_the_best_of_the_whole_pool() -> None:
    pool = [candidate(70 + i, portal=i % 2, key=f"k{i}") for i in range(10)]

    selection = select_global_top(pool, portal_count=2, requested_limit=4)

    assert [c.final_score for c in selection.selected] == [79, 78, 77, 76]
    assert selection.pool_size == 10


def test_one_portal_may_take_every_place() -> None:
    """No reserved slots: if a portal holds the best jobs, it supplies them."""
    pool = [candidate(90 + i, portal=0, key=f"a{i}") for i in range(5)]
    pool += [candidate(71, portal=1, key="b1")]

    selection = select_global_top(pool, portal_count=2, requested_limit=5)

    assert selection.per_portal_selected == (5, 0)
    assert selection.per_portal_available == (5, 1)


def test_a_thin_portal_holds_no_slots_open() -> None:
    pool = [candidate(80 + i, portal=0, key=f"a{i}") for i in range(30)]
    pool += [candidate(99, portal=1, key="b1")]

    selection = select_global_top(pool, portal_count=2, requested_limit=20)

    assert selection.returned_count == 20
    assert selection.per_portal_selected[1] == 1
    assert selection.per_portal_selected[0] == 19


def test_the_order_does_not_depend_on_the_input_order() -> None:
    pool = [candidate(70 + (i % 7), portal=i % 3, key=f"k{i}") for i in range(30)]

    forward = select_global_top(pool, portal_count=3, requested_limit=10)
    backward = select_global_top(
        list(reversed(pool)), portal_count=3, requested_limit=10
    )

    assert [c.job_id for c in forward.selected] == [c.job_id for c in backward.selected]


def test_selection_uses_the_existing_ranking_unchanged() -> None:
    """Phase 12 changed when jobs are ranked, not how."""
    pool = [candidate(70 + (i % 5), portal=i % 2, key=f"k{i}") for i in range(20)]

    selection = select_global_top(pool, portal_count=2, requested_limit=8)

    assert list(selection.selected) == order_candidates(pool)[:8]


def test_an_empty_pool_returns_nothing_rather_than_failing() -> None:
    selection = select_global_top([], portal_count=3, requested_limit=20)

    assert selection.selected == ()
    assert selection.pool_size == 0
    assert selection.per_portal_available == (0, 0, 0)


def test_a_pool_smaller_than_the_request_is_returned_whole() -> None:
    """The threshold is never lowered to fill a count."""
    pool = [candidate(75, portal=0, key=f"k{i}") for i in range(3)]

    selection = select_global_top(pool, portal_count=1, requested_limit=100)

    assert selection.returned_count == 3
