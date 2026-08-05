"""How deep one portal is allowed to search, and why it stopped.

Phase 12 replaces the quota-driven search with a deep unified one. Under quotas
a portal stopped as soon as it had filled its share, which meant the run's depth
depended on how many portals were selected: with five portals and Find 20 each
portal collected roughly sixteen listings and stopped, whatever else it had.

Now every portal searches independently until it is genuinely finished. "Until
exhausted" still has to be bounded, or a slow portal would hold the request open
indefinitely, so the bound moved from *how many jobs the run still wants* to
*how much work this portal may do* - which is a property of the portal, not of
the other portals' luck.

Four independent limits, any of which stops collection:

* ``max_pages`` - passes over the freshness ladder, one per window
* ``max_requests`` - adapter calls, the closest proxy to load on the portal
* ``max_runtime_seconds`` - wall clock for this portal's whole leg
* ``max_jobs`` - listings collected, which bounds memory

Every limit is read from that portal's ``portal_configs.options`` row, so depth
is operational configuration and not a deployment. The defaults are deliberately
generous compared with the quota era but still finite.

``ExhaustionReason`` records which of them ended the leg - or that the portal
simply ran out of listings, which is the outcome this phase is designed to reach
and the one the run summary reports as ``exhausted``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ExhaustionReason(StrEnum):
    """Why one portal stopped collecting."""

    #: The portal ran out of listings. The goal state: nothing was left behind.
    EXHAUSTED = "exhausted"
    MAX_PAGES = "max_pages"
    MAX_REQUESTS = "max_requests"
    MAX_JOBS = "max_jobs"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"

    @property
    def is_complete(self) -> bool:
        """True when the portal had nothing further to give.

        Anything else means the run stopped early and may have left qualifying
        jobs uncollected, which the summary states rather than hides.
        """
        return self is ExhaustionReason.EXHAUSTED


#: Windows in the freshness ladder. One pass per window is the natural maximum,
#: so this is the default page cap.
DEFAULT_MAX_PAGES = 6

#: Adapter calls per portal per run. Six windows times six same-role queries is
#: thirty-six; this leaves room for a portal that paginates within a window.
DEFAULT_MAX_REQUESTS = 60

#: Wall clock for one portal's collection. The whole run is still synchronous,
#: but portals now collect concurrently, so this is a per-portal ceiling rather
#: than a share of a single budget.
DEFAULT_MAX_RUNTIME_SECONDS = 150.0

#: Listings one portal may collect in a run. Bounds memory: collection now
#: gathers into memory before anything is written. Measured against the live
#: portals, 200 is far past what any of them contributes to a Find 100 after
#: the US filter, deduplication and the threshold have run.
DEFAULT_MAX_JOBS = 200

#: Ceiling handed to a single adapter call. Portals that paginate internally use
#: it to decide how many pages to walk.
DEFAULT_PER_REQUEST_LIMIT = 100

#: Ceiling on one adapter call. Deliberately generous: one call is not always
#: one HTTP request. Greenhouse fans a single ``search`` out across every
#: configured board, and with portals now collecting concurrently those requests
#: compete for bandwidth - measured live, that call needs well over the thirty
#: seconds a single-request portal does.
DEFAULT_QUERY_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True, slots=True)
class SearchDepth:
    """One portal's collection limits for one run."""

    max_pages: int = DEFAULT_MAX_PAGES
    max_requests: int = DEFAULT_MAX_REQUESTS
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS
    max_jobs: int = DEFAULT_MAX_JOBS
    per_request_limit: int = DEFAULT_PER_REQUEST_LIMIT
    query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS

    @classmethod
    def from_options(cls, options: dict[str, Any] | None) -> SearchDepth:
        """Build from a portal's configuration, ignoring unknown keys.

        A value that is missing, the wrong type or not positive falls back to
        the default rather than disabling the limit: a misconfigured row must
        not be able to create an unbounded search.
        """
        options = options or {}
        values: dict[str, Any] = {}

        for name, spec in cls.__dataclass_fields__.items():  # type: ignore[attr-defined]
            raw = options.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                continue
            if raw <= 0:
                continue
            # The field's declared default carries the type. Reading it off the
            # class would return the slot descriptor, not the value.
            values[name] = float(raw) if isinstance(spec.default, float) else int(raw)

        return cls(**values)

    def request_budget_reached(self, requests: int) -> bool:
        return requests >= self.max_requests

    def job_budget_reached(self, collected: int) -> bool:
        return collected >= self.max_jobs


__all__ = [
    "DEFAULT_MAX_JOBS",
    "DEFAULT_QUERY_TIMEOUT_SECONDS",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_MAX_RUNTIME_SECONDS",
    "DEFAULT_PER_REQUEST_LIMIT",
    "ExhaustionReason",
    "SearchDepth",
]
