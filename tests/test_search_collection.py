"""Driving one adapter through the freshness ladder.

No database and no network: these assert how the collector treats an adapter,
which is what decides whether a portal is asked for the same page twice.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import FRESHNESS_ESCALATION, FreshnessWindow
from app.portals.exceptions import PortalError, PortalParseError, PortalTimeoutError
from app.search.collection import PortalCollector
from tests.fake_portals import build_fake_portal, make_raw_job

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

QUERIES = ("Financial Analyst", "FP&A Analyst")


def jobs_at_ages(portal: str, *ages: float) -> list:
    return [
        make_raw_job(portal=portal, index=index, age_days=age, now=NOW)
        for index, age in enumerate(ages)
    ]


async def drain(collector: PortalCollector, *, windows=FRESHNESS_ESCALATION) -> list:
    return [batch async for batch in collector.windows(windows=windows, now=NOW)]


# --- Portals that cannot filter by date -------------------------------------


async def test_a_portal_without_a_date_filter_is_asked_once() -> None:
    # Jobright is in this group: its pages ignore date filters and paging, so
    # asking again would return the identical page and spend a request for
    # nothing.
    adapter_class = build_fake_portal("no-filter", jobs=jobs_at_ages("p", 0.5, 5, 40))
    adapter = adapter_class()

    batches = await drain(PortalCollector(adapter, queries=QUERIES, per_window_limit=50))

    # One call per query, once - not once per query per window.
    assert len(adapter.calls) == len(QUERIES)
    assert sum(1 for batch in batches if batch.fetched) == 1


async def test_widening_admits_older_listings_locally() -> None:
    adapter_class = build_fake_portal("local", jobs=jobs_at_ages("p", 0.5, 5, 40))
    adapter = adapter_class()

    batches = {
        batch.window: batch
        async for batch in PortalCollector(
            adapter, queries=("Financial Analyst",), per_window_limit=50
        ).windows(now=NOW)
    }

    assert len(batches[FreshnessWindow.LAST_24H].jobs) == 1
    assert len(batches[FreshnessWindow.LAST_7D].jobs) == 1
    assert len(batches[FreshnessWindow.OLDER_ACTIVE].jobs) == 1


async def test_each_listing_is_handed_over_exactly_once() -> None:
    adapter_class = build_fake_portal("once", jobs=jobs_at_ages("p", 0.1, 0.2, 0.3))
    adapter = adapter_class()

    batches = await drain(
        PortalCollector(adapter, queries=("Financial Analyst",), per_window_limit=50)
    )

    identities = [job.external_id for batch in batches for job in batch.jobs]
    assert sorted(identities) == ["p-0", "p-1", "p-2"]
    assert len(identities) == len(set(identities))


async def test_an_undated_listing_arrives_in_the_final_window() -> None:
    undated = make_raw_job(portal="p", index=0, now=NOW)
    undated.posted_at = None
    adapter_class = build_fake_portal("undated", jobs=[undated])
    adapter = adapter_class()

    batches = {
        batch.window: batch
        async for batch in PortalCollector(
            adapter, queries=("Financial Analyst",), per_window_limit=10
        ).windows(now=NOW)
    }

    assert batches[FreshnessWindow.LAST_24H].jobs == []
    assert len(batches[FreshnessWindow.OLDER_ACTIVE].jobs) == 1


# --- Portals that can filter by date ----------------------------------------


async def test_a_portal_with_a_date_filter_is_asked_per_window() -> None:
    adapter_class = build_fake_portal(
        "filtered", jobs=jobs_at_ages("p", 0.5), date_filter=True
    )
    adapter = adapter_class()

    windows = FRESHNESS_ESCALATION[:3]
    batches = await drain(
        PortalCollector(adapter, queries=("Financial Analyst",), per_window_limit=10),
        windows=windows,
    )

    assert len(adapter.calls) == len(windows)
    assert [call.window for call in adapter.calls] == list(windows)
    assert all(batch.fetched for batch in batches)


async def test_a_filtered_portal_still_yields_each_listing_once() -> None:
    # The same job legitimately appears in the 24h and 7d responses.
    adapter_class = build_fake_portal(
        "repeat", jobs=jobs_at_ages("p", 0.5), date_filter=True
    )
    adapter = adapter_class()

    batches = await drain(
        PortalCollector(adapter, queries=("Financial Analyst",), per_window_limit=10)
    )

    assert sum(len(batch.jobs) for batch in batches) == 1


# --- Bookkeeping and isolation ----------------------------------------------


async def test_every_executed_query_is_reported() -> None:
    adapter_class = build_fake_portal("audit", jobs=jobs_at_ages("p", 0.5))
    adapter = adapter_class()

    batches = await drain(
        PortalCollector(adapter, queries=QUERIES, per_window_limit=10)
    )
    executed = [item for batch in batches for item in batch.executed]

    assert [item.query_text for item in executed] == list(QUERIES)
    assert all(item.duration_ms >= 0 for item in executed)
    assert all(item.executed_at.tzinfo is not None for item in executed)


async def test_no_queries_means_no_requests() -> None:
    adapter_class = build_fake_portal("silent", jobs=jobs_at_ages("p", 0.5))
    adapter = adapter_class()

    batches = await drain(PortalCollector(adapter, queries=(), per_window_limit=10))

    assert batches == []
    assert adapter.calls == []


async def test_a_portal_error_is_raised_not_swallowed() -> None:
    # An empty result would read as "this portal has no matching jobs", which
    # is a different and misleading claim.
    adapter_class = build_fake_portal(
        "broken", error=PortalParseError("shape changed", portal="broken")
    )
    adapter = adapter_class()

    with pytest.raises(PortalParseError):
        await drain(PortalCollector(adapter, queries=QUERIES, per_window_limit=10))


async def test_a_slow_portal_times_out() -> None:
    class Slow:
        slug = "slow"

        @property
        def capabilities(self):
            from app.portals.base import PortalCapabilities

            return PortalCapabilities()

        async def search(self, request):
            await asyncio.sleep(5)

    with pytest.raises(PortalTimeoutError):
        await drain(
            PortalCollector(
                Slow(),  # type: ignore[arg-type]
                queries=("Financial Analyst",),
                per_window_limit=10,
                query_timeout_seconds=0.05,
            )
        )


async def test_an_unexpected_adapter_exception_becomes_a_portal_error() -> None:
    # A misbehaving adapter must still be isolated to its own portal.
    adapter_class = build_fake_portal("rude", error=RuntimeError("boom"))
    adapter = adapter_class()

    with pytest.raises(PortalError):
        await drain(PortalCollector(adapter, queries=QUERIES, per_window_limit=10))


async def test_the_per_window_limit_is_respected() -> None:
    adapter_class = build_fake_portal(
        "many",
        jobs=[
            make_raw_job(portal="p", index=index, age_days=0.1, now=NOW)
            for index in range(30)
        ],
    )
    adapter = adapter_class()

    batches = await drain(
        PortalCollector(adapter, queries=("Financial Analyst",), per_window_limit=5)
    )

    assert all(len(batch.jobs) <= 5 for batch in batches)


async def test_an_exhausted_budget_stops_the_ladder() -> None:
    # A synchronous request has to return, so a portal that spends its whole
    # budget is cut off rather than allowed to hold the search open.
    class Sluggish:
        slug = "sluggish"

        def __init__(self) -> None:
            self.calls = 0

        @property
        def capabilities(self):
            from app.portals.base import PortalCapabilities

            return PortalCapabilities(supports_date_filter=True)

        async def search(self, request):
            from app.portals.base import PortalSearchResult

            self.calls += 1
            await asyncio.sleep(0.08)
            return PortalSearchResult(jobs=[], window=request.window)

    adapter = Sluggish()
    collector = PortalCollector(
        adapter,  # type: ignore[arg-type]
        queries=("Financial Analyst",),
        per_window_limit=10,
        budget_seconds=0.03,
    )
    batches = [batch async for batch in collector.windows(now=NOW)]

    # The first window is fetched, then the budget check stops the ladder well
    # short of all six windows.
    assert adapter.calls == 1
    assert len(batches) < len(FRESHNESS_ESCALATION)


async def test_a_non_positive_budget_means_no_limit() -> None:
    adapter_class = build_fake_portal("unbounded", jobs=jobs_at_ages("p", 0.5))
    adapter = adapter_class()

    collector = PortalCollector(
        adapter,
        queries=("Financial Analyst",),
        per_window_limit=10,
        budget_seconds=0.0,
    )
    batches = [batch async for batch in collector.windows(now=NOW)]

    assert len(batches) == len(FRESHNESS_ESCALATION)
