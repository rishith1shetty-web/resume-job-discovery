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
