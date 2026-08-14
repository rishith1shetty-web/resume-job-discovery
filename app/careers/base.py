"""What a career-page source is, and what it returns.

The contract mirrors ``app.portals.base`` in shape so the two read alike, but
the payload is deliberately different. A ``CareerPage`` is a *company* and the
recruiting page it publishes - not a vacancy - and it is typed that way so no
later change can quietly feed one into the job pipeline.

# Why a separate registry

``app.portals.registry`` maps a slug to a ``BasePortal``. A career source is
not a ``BasePortal``: it has no ``search`` returning ``RawJob``, no freshness
window, no pagination cursor. Forcing it into that interface would mean
implementing three methods with lies in them. A second, tiny registry keeps the
resolution rule the same - *ask a registry, never branch on a name* - while
letting each contract stay honest about what it can do.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, TypeVar

from app.core.constants import JobUrlKind
from app.core.logging import get_logger
from app.matching.profile import ResumeMatchProfile

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CompanyOpening:
    """One live posting, used only as evidence that a company is hiring.

    Never returned to the user as a result and never scored as a job. It exists
    so a career page can say *why* it is being shown - "3 current US openings
    matching FP&A Analyst" is a claim about postings that were actually seen.
    """

    title: str
    location: str | None
    application_url: str | None
    #: Which portal surfaced the posting. Recorded so the evidence is traceable.
    source: str
    posted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CareerPage:
    """One company and its official career page.

    ``relevance`` is a company-level judgement, not a resume match score. It
    answers "does this employer look worth opening?" from the openings actually
    observed, and is deliberately named so it cannot be mistaken for the 0-100
    ``final_score`` a scored vacancy carries.
    """

    company_name: str
    #: Normalised comparison key. Two records with the same key are one company.
    company_key: str
    relevant_role: str
    location: str
    official_career_url: str
    #: Why the URL was accepted: the employer's own domain, or their own space
    #: on an applicant-tracking system.
    url_kind: JobUrlKind
    #: How many role-matching US openings were seen for this company.
    opening_count: int
    relevance: float
    reason: str
    #: Portals that contributed evidence, for traceability.
    sources: tuple[str, ...] = ()
    #: Titles seen, newest evidence first. Shown as supporting detail.
    matched_titles: tuple[str, ...] = ()

    @property
    def is_employer_domain(self) -> bool:
        return self.url_kind is JobUrlKind.EMPLOYER_CAREER


@dataclass(frozen=True, slots=True)
class CareerPageRequest:
    """What one career-page discovery pass is looking for.

    ``queries`` are the same same-role title variations the portals receive, so
    a Financial Analyst resume cannot widen into Accountant or Business Analyst
    here any more than it can there (CLAUDE.md sections 3 and 18).
    """

    queries: tuple[str, ...]
    role_display: str
    role_key: str
    limit: int
    #: The analysed resume, carried so the role gate applied to a career page
    #: is the same one applied to a vacancy rather than a second, looser copy.
    profile: ResumeMatchProfile | None = None


@dataclass(slots=True)
class CareerPageDiscovery:
    """The outcome of one discovery pass, with what it refused and why."""

    pages: list[CareerPage] = field(default_factory=list)
    #: Distinct companies seen before verification and relevance.
    companies_seen: int = 0
    #: Companies dropped because no official career URL could be verified.
    unverified_companies: int = 0
    #: Openings dropped by the US-only rule.
    non_us_openings: int = 0
    #: Openings dropped because the title was not the resume's role.
    off_role_openings: int = 0
    #: Companies merged into another under name normalisation.
    merged_companies: int = 0
    error: Exception | None = None

    @property
    def returned_count(self) -> int:
        return len(self.pages)


class CareerPageSource(ABC):
    """Interface every career-page source implements."""

    #: Stable identifier; must match ``portals.slug`` for the row that selects it.
    slug: ClassVar[str]
    display_name: ClassVar[str]

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}

    @abstractmethod
    async def discover(
        self,
        request: CareerPageRequest,
        *,
        listings: Sequence[Any],
        now: datetime,
    ) -> CareerPageDiscovery:
        """Find companies and their official career pages.

        ``listings`` are the raw postings already collected in this run, which
        the source may use as evidence rather than re-fetching. A source must
        return a discovery rather than raise for an ordinary empty result;
        raising is reserved for the source itself being broken, which the run
        records as a failed leg.
        """


CareerSourceT = TypeVar("CareerSourceT", bound=type[CareerPageSource])


class CareerSourceRegistry:
    """Maps ``slug -> career source class``.

    Same shape and same purpose as ``PortalRegistry``: it is the indirection
    that keeps source names out of the orchestrator. Not a global singleton by
    accident - tests build their own.
    """

    def __init__(self) -> None:
        self._sources: dict[str, type[CareerPageSource]] = {}

    def register(self, source: CareerSourceT) -> CareerSourceT:
        slug = getattr(source, "slug", None)
        if not slug:
            raise ValueError(f"{source.__name__} must define a non-empty `slug`.")
        if slug in self._sources and self._sources[slug] is not source:
            raise ValueError(
                f"Career source slug {slug!r} is already registered to "
                f"{self._sources[slug].__name__}."
            )
        self._sources[slug] = source
        logger.debug("career_source_registered", slug=slug)
        return source

    def unregister(self, slug: str) -> None:
        self._sources.pop(slug, None)

    def is_registered(self, slug: str) -> bool:
        return slug in self._sources

    def get(self, slug: str) -> type[CareerPageSource]:
        try:
            return self._sources[slug]
        except KeyError:
            raise KeyError(f"No career source registered for {slug!r}.") from None

    def create(
        self, slug: str, options: dict[str, Any] | None = None
    ) -> CareerPageSource:
        return self.get(slug)(options=options)

    @property
    def slugs(self) -> tuple[str, ...]:
        return tuple(sorted(self._sources))

    def __contains__(self, slug: object) -> bool:
        return isinstance(slug, str) and slug in self._sources

    def __len__(self) -> int:
        return len(self._sources)


#: Process-wide registry, populated by importing ``app.careers``.
career_registry = CareerSourceRegistry()


__all__ = [
    "CareerPage",
    "CareerPageDiscovery",
    "CareerPageRequest",
    "CareerPageSource",
    "CareerSourceRegistry",
    "CompanyOpening",
    "career_registry",
]
