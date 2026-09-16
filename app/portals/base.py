"""Portal adapter contract (CLAUDE.md section 4).

Phase 1 defines the interface only. No adapter exists yet and no scraping,
HTTP calls or portal-specific logic belong in this package until Phase 4.

Isolation rule: the core search service depends on ``BasePortal`` and
``PortalRegistry`` and nothing else from this package. It never imports a
concrete adapter and never branches on a portal name.

Every adapter returns the same normalised ``RawJob``, so downstream
normalisation, scoring and deduplication are entirely portal-agnostic.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from app.core.constants import ApplicationType, FreshnessWindow


#: The one ``raw_payload`` key the core is allowed to read (Phase 13).
#:
#: The field set of ``RawJob`` is fixed by CLAUDE.md section 4 and adapters may
#: not extend it, so a portal declares how much of an application a listing
#: demands through this reserved key instead. It is part of the *contract*, not
#: a portal-specific key: ``app.processing.application_type`` reads this name
#: and no other, so normalisation stays portal-agnostic and a new portal is
#: filtered on day one without the core learning anything about it.
#:
#: Set it with ``declare_application_type``. Leaving it unset is legitimate and
#: means "this portal cannot tell" - the shared classifier then looks for
#: evidence itself and, finding none, returns ``UNKNOWN``.
APPLICATION_TYPE_KEY = "application_type"


def declare_application_type(
    payload: dict[str, Any], application_type: ApplicationType
) -> dict[str, Any]:
    """Record a portal's own verdict on a listing's application style.

    Returns the payload so it can be used inline when building a ``RawJob``.
    Adapters should only call this when the portal published something real -
    a flag, a source type, a destination it owns. Declaring a type the portal
    did not actually state would be guessing, which this phase forbids.
    """
    payload[APPLICATION_TYPE_KEY] = ApplicationType(application_type).value
    return payload


@dataclass(slots=True)
class RawJob:
    """A single listing as returned by a portal, before normalisation.

    Adapters do no scoring, no filtering and no deduplication - they collect
    and hand back exactly what the portal exposed. ``raw_payload`` preserves
    the untouched source response so a later scoring version can re-extract
    fields without re-fetching.

    ``raw_payload`` may also carry the reserved ``APPLICATION_TYPE_KEY``, which
    is how an adapter tells the core what the portal said about applying. See
    that constant; every other key in the payload stays opaque to the core.
    """

    external_id: str | None
    source: str
    title: str
    company: str | None
    location: str | None
    description: str | None
    source_url: str
    application_url: str | None
    posted_at: datetime | None
    employment_type: str | None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Deadline:
    """How much wall clock one adapter call may still spend.

    An adapter that fans out - over employer boards, tenants or pages - does
    many requests inside a single ``search``. Without this it cannot know that
    the caller is about to give up, so it keeps issuing requests until it is
    cancelled, and cancellation destroys everything it had already collected.
    Twenty boards read successfully are lost because the twenty-first was slow.

    A deadline turns that into a decision the adapter can make: before starting
    another expensive request, ask whether there is time for it, and if not
    return what is already in hand and say the result is partial. The caller
    keeps the jobs; only the unvisited boards are lost, which is the part that
    was never going to arrive anyway.

    ``reserve`` is the margin held back so the adapter can finish assembling
    and return *before* the caller's hard timeout fires. It is not a guarantee:
    a request already in flight can still overrun, and the hard timeout remains
    as the backstop. It makes the cooperative path the normal one.
    """

    #: A ``time.monotonic`` reading, not a wall-clock time.
    expires_at: float
    reserve: float = 0.0

    @classmethod
    def in_seconds(cls, seconds: float, *, reserve: float = 0.0) -> Deadline:
        return cls(expires_at=time.monotonic() + seconds, reserve=reserve)

    @property
    def remaining(self) -> float:
        """Seconds left before the adapter should be returning, never below 0."""
        return max(0.0, self.expires_at - self.reserve - time.monotonic())

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    def allows(self, cost_seconds: float) -> bool:
        """Whether a request expected to take ``cost_seconds`` should start.

        Adapters use this for work whose cost they can estimate. When they
        cannot, ``expired`` is the honest check and this one is not used.
        """
        return self.remaining > max(0.0, cost_seconds)


@dataclass(frozen=True, slots=True)
class PortalCapabilities:
    """What a portal can actually do.

    The search planner reads these to decide whether it can trust a portal's
    date filtering or must fetch descriptions separately.
    """

    supports_date_filter: bool = False
    supports_description_fetch: bool = False
    supports_pagination: bool = True
    requires_auth: bool = False
    provides_application_url: bool = False


@dataclass(frozen=True, slots=True)
class PortalSearchRequest:
    """One query pass against one portal, in one freshness window.

    ``queries`` holds same-role title variations only (CLAUDE.md section 3).
    Unrelated-role broadening is never performed.
    """

    queries: tuple[str, ...]
    window: FreshnessWindow
    limit: int
    location: str | None = None
    remote_only: bool = False
    cursor: str | None = None
    #: How long this call may still run, when the caller imposes one. Optional
    #: by design: an adapter that ignores it behaves exactly as before and is
    #: still bounded by the caller's hard timeout. Adapters that fan out should
    #: honour it - see ``Deadline``.
    deadline: Deadline | None = None


@dataclass(slots=True)
class PortalSearchResult:
    """Outcome of one search pass. ``next_cursor`` drives pagination."""

    jobs: list[RawJob] = field(default_factory=list)
    window: FreshnessWindow = FreshnessWindow.LAST_24H
    has_more: bool = False
    next_cursor: str | None = None
    # True when the portal applied the date filter itself; when False the
    # caller must filter by posted_at locally.
    window_applied_by_portal: bool = False
    #: True when the adapter stopped early against ``request.deadline`` and
    #: there was more it could have read. The jobs are complete and valid; the
    #: *sweep* is not, and the run says so rather than reporting this portal as
    #: having nothing further to give.
    partial: bool = False


def out_of_time(request: PortalSearchRequest) -> bool:
    """Whether an adapter should stop before its next expensive request.

    The one line every fan-out loop shares. An adapter with no deadline - a
    direct caller, a test, a health check - is never out of time, so adding
    this guard cannot change how an adapter behaves for anyone who does not
    impose one.

    Placed *before* a request, never after: the point is to avoid starting work
    that cannot finish, so that what has already been collected can be returned
    rather than destroyed by cancellation.
    """
    return request.deadline is not None and request.deadline.expired


@dataclass(frozen=True, slots=True)
class PortalHealth:
    is_available: bool
    checked_at: datetime
    detail: str | None = None


class BasePortal(ABC):
    """Interface every portal adapter implements.

    Adapters are constructed with their ``PortalConfig`` options, so timeouts,
    retries and rate limits come from the database rather than from code.
    """

    #: Stable identifier; must match ``portals.slug``.
    slug: ClassVar[str]
    #: Human-readable name for logs and the dashboard.
    display_name: ClassVar[str]

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}

    @property
    @abstractmethod
    def capabilities(self) -> PortalCapabilities:
        """Declare what this portal supports."""

    @abstractmethod
    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings for one query pass.

        Must raise a ``PortalError`` subclass on failure rather than returning
        an empty result, so the run can record the error and isolate it to
        this portal (CLAUDE.md section 18).
        """

    @abstractmethod
    async def fetch_description(self, job: RawJob) -> str | None:
        """Fetch the full description when ``search`` returned only a snippet.

        Returns None when unavailable; the matching layer then decides whether
        the job can be scored reliably at all.
        """

    @abstractmethod
    async def validate_url(self, url: str) -> bool:
        """Report whether the listing is still reachable and active."""

    async def health_check(self) -> PortalHealth:
        """Cheap reachability probe. Adapters may override with a real check."""
        from datetime import UTC

        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))
