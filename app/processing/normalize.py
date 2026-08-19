"""Turn one ``RawJob`` into normalised values ready to be stored or compared.

This is the single entry point the processing service uses. It is entirely
portal-agnostic: it reads the ``RawJob`` contract every adapter already returns
and never looks inside ``raw_payload`` for portal-specific keys, so adding a
portal never means editing this file (CLAUDE.md section 4).

A listing that cannot be normalised is *rejected*, not repaired. Inventing a
company or a location to make a record storable would put a job in front of the
user that no portal actually published.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from app.core.constants import JobUrlKind, WorkMode
from app.core.logging import get_logger
from app.portals.base import RawJob
from app.processing.application_type import (
    QUICK_APPLY_REJECTION,
    classify_application_type,
)
from app.processing.companies import NormalizedCompany, normalize_company
from app.processing.locations import NormalizedLocation, normalize_location
from app.processing.us_locations import NON_US_REJECTION, classify_us_location
from app.processing.text import (
    clean_block_text,
    clean_display_text,
    fingerprint,
    stable_hash,
)
from app.processing.titles import NormalizedTitle, detect_work_mode, normalize_title
from app.processing.urls import NormalizedUrl, normalize_and_classify

logger = get_logger(__name__)

#: Longest a title or company may be, matching the database columns.
#:
#: These are enforced, not advisory. Postgres does not silently truncate an
#: over-long value: it raises, and because a portal's listings are written
#: inside that portal's savepoint the raise rolls back the whole leg - so one
#: unusual record costs every listing the portal collected and the portal
#: reports zero. That is exactly what a Hiring Cafe posting advertised across
#: twenty-two cities did, with a 282-character location.
#:
#: Enforcing them here rather than widening the columns is deliberate: a
#: location that long is a list of places, not a place, and the column width is
#: not what makes it unusable for matching.
MAX_TITLE_LENGTH = 512
MAX_COMPANY_LENGTH = 256
MAX_LOCATION_LENGTH = 256
MAX_EMPLOYMENT_TYPE_LENGTH = 64

#: Clock skew tolerated on a posting date before it is treated as unusable.
#: Portals occasionally publish a date a few hours ahead; a date weeks in the
#: future is broken data, and storing it would put the job at the top of every
#: "newest first" sort forever.
MAX_FUTURE_SKEW = timedelta(days=2)

#: Nothing in this product predates it; anything older is a parsing failure.
EARLIEST_PLAUSIBLE_POSTING = datetime(2000, 1, 1, tzinfo=UTC)

# Employment types folded to one spelling. Unrecognised values are kept as the
# portal wrote them rather than dropped - the portal knows its own vocabulary.
_EMPLOYMENT_TYPES: dict[str, str] = {
    "full time": "Full-time",
    "fulltime": "Full-time",
    "full": "Full-time",
    "ft": "Full-time",
    "permanent": "Full-time",
    "part time": "Part-time",
    "parttime": "Part-time",
    "pt": "Part-time",
    "contract": "Contract",
    "contractor": "Contract",
    "contract to hire": "Contract",
    "c2h": "Contract",
    "temporary": "Temporary",
    "temp": "Temporary",
    "seasonal": "Temporary",
    "internship": "Internship",
    "intern": "Internship",
    "apprenticeship": "Apprenticeship",
    "volunteer": "Volunteer",
    "freelance": "Freelance",
}


class RejectionReason:
    """Why a raw capture could not become a job. Stored on ``raw_jobs``."""

    NO_TITLE = "no_usable_title"
    NO_SOURCE_URL = "no_valid_source_url"
    #: Phase 10: this product is locked to United States vacancies. Applied
    #: here, during normalisation, so a non-US listing is never scored, never
    #: deduplicated and never takes a portal's quota slot.
    NON_US_LOCATION = NON_US_REJECTION
    #: Phase 13: only jobs asking for a real application are returned. Applied
    #: immediately after the US filter, so a one-click listing is never scored,
    #: never canonicalised and never deduplicated against a real vacancy.
    QUICK_APPLY = QUICK_APPLY_REJECTION


@dataclass(frozen=True, slots=True)
class NormalizedJob:
    """One listing, normalised. Display values and comparison keys side by side."""

    portal_slug: str
    external_id: str | None

    title: NormalizedTitle
    company: NormalizedCompany | None
    location: NormalizedLocation
    work_mode: WorkMode | None
    employment_type: str | None

    description: str | None
    description_fingerprint: str | None

    source_url: NormalizedUrl
    application_url: NormalizedUrl | None
    posted_at: datetime | None

    canonical_key: str

    @property
    def company_key(self) -> str:
        return self.company.key if self.company else ""

    @property
    def preferred_url(self) -> NormalizedUrl:
        """The most direct usable link this single source offers.

        Falls back to the listing URL: every source must give the user
        something to open, and a listing page is a usable manual route.
        """
        if self.application_url is None:
            return self.source_url
        if self.application_url.preference_rank <= self.source_url.preference_rank:
            return self.application_url
        return self.source_url

    @property
    def candidate_canonical_urls(self) -> tuple[str, ...]:
        """Every canonical URL this source contributes, for comparison."""
        urls = {self.source_url.canonical}
        if self.application_url is not None:
            urls.add(self.application_url.canonical)
        return tuple(sorted(urls))

    @property
    def candidate_stored_urls(self) -> tuple[str, ...]:
        """The URLs as they are written to ``job_sources``.

        Distinct from the canonical forms: stored URLs stay openable, so a
        lookup against the database has to use these and not the comparison
        keys, which force https and drop ``www.``.
        """
        urls = {self.source_url.url}
        if self.application_url is not None:
            urls.add(self.application_url.url)
        return tuple(sorted(urls))


@dataclass(frozen=True, slots=True)
class RejectedJob:
    """A capture that could not be normalised, with the reason recorded."""

    portal_slug: str
    external_id: str | None
    title: str | None
    reason: str


#: Characters and words a trimmed value must not be left dangling on, so a
#: shortened list of places reads as a list that stopped rather than as
#: corrupted text.
_TRAILING_PUNCTUATION = " ,;|/-&"
_JOINERS = frozenset({"", "or", "and"})


def clamp_display(value: str | None, limit: int) -> str | None:
    """Trim a user-facing value to its column width, on a word boundary.

    A hard slice would leave a location reading "... or Kansas City or Albuqu",
    which looks like corrupted data rather than a shortened list. Cutting back
    to the last whole word avoids that, and the trailing separator goes with it
    so the result does not end in "or" or a stray comma.

    Falls back to the hard slice when there is no sensible boundary - a single
    word longer than the column has to be cut somewhere.
    """
    if value is None or len(value) <= limit:
        return value

    cut = value[:limit].rstrip()
    boundary = cut.rfind(" ")
    if boundary >= limit // 2:
        cut = cut[:boundary]

    words = cut.split(" ")
    while words and words[-1].strip(_TRAILING_PUNCTUATION).casefold() in _JOINERS:
        words.pop()

    return " ".join(words).rstrip(_TRAILING_PUNCTUATION).rstrip() or value[:limit]


def clamp_key(value: str, limit: int) -> str:
    """Trim a comparison key to its column width.

    Sliced rather than word-trimmed: a key is compared, never read, and the cut
    has to be the same every time so a job keeps its identity between runs.
    """
    return value[:limit]


def normalize_employment_type(value: str | None) -> str | None:
    """Fold an employment type to one spelling, or keep the portal's wording."""
    cleaned = clean_display_text(value)
    if not cleaned:
        return None

    folded = _EMPLOYMENT_TYPES.get(cleaned.casefold().replace("-", " ").replace("_", " "))
    result = folded or cleaned

    return result[:MAX_EMPLOYMENT_TYPE_LENGTH]


def normalize_posted_at(value: datetime | None, *, now: datetime | None = None) -> datetime | None:
    """Return a timezone-aware UTC posting date, or None when it is unusable.

    A naive datetime is read as UTC: every adapter is required to work in UTC,
    and guessing a local zone would shift the date by up to a day.
    """
    if value is None:
        return None

    moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)

    reference = now or datetime.now(UTC)
    if moment > reference + MAX_FUTURE_SKEW:
        return None
    if moment < EARLIEST_PLAUSIBLE_POSTING:
        return None

    return moment


def build_canonical_key(
    *,
    company_key: str,
    title_key: str,
    location_key: str,
    level: int | None,
    fallback: str,
) -> str:
    """The grouping identity for a canonical job.

    Deliberately date-free, so the same vacancy carried by two portals with
    different "posted" dates produces one key. A repost of the same role at the
    same employer and place is treated as that same vacancy re-advertised, with
    ``posted_at`` and ``last_seen_at`` carrying the timeline - which is exactly
    the probable-duplicate rule in CLAUDE.md section 9.

    Without an employer there is nothing to group on, so the key falls back to
    the listing URL and the job stays separate rather than merging with every
    other company-less posting that shares its title.
    """
    if not company_key:
        return stable_hash("nocompany", title_key, fallback)

    return stable_hash(company_key, title_key, location_key, str(level or ""))


def normalize_raw_job(
    raw: RawJob, *, now: datetime | None = None
) -> NormalizedJob | RejectedJob:
    """Normalise one collected listing.

    Returns a ``RejectedJob`` rather than raising: one unusable record must not
    stop the rest of a portal's batch from being processed.
    """
    portal_slug = raw.source

    title = normalize_title(raw.title)
    if title is None:
        return RejectedJob(
            portal_slug=portal_slug,
            external_id=raw.external_id,
            title=raw.title,
            reason=RejectionReason.NO_TITLE,
        )
    title = replace(
        title,
        display=clamp_display(title.display, MAX_TITLE_LENGTH) or title.display,
        key=clamp_key(title.key, MAX_TITLE_LENGTH),
    )

    company = normalize_company(raw.company)
    if company is not None:
        company = replace(
            company,
            display=clamp_display(company.display, MAX_COMPANY_LENGTH)
            or company.display,
            key=clamp_key(company.key, MAX_COMPANY_LENGTH),
        )

    source_url = normalize_and_classify(
        raw.source_url,
        company=company.display if company else None,
        portal_slug=portal_slug,
    )
    if source_url is None:
        return RejectedJob(
            portal_slug=portal_slug,
            external_id=raw.external_id,
            title=title.display,
            reason=RejectionReason.NO_SOURCE_URL,
        )

    application_url = normalize_and_classify(
        raw.application_url,
        company=company.display if company else None,
        portal_slug=portal_slug,
    )

    description = clean_block_text(raw.description)
    location = normalize_location(raw.location, extra_mode_text=title.display)
    # Clamped before the US gate reads it, so what is judged is what is stored.
    location = replace(
        location, display=clamp_display(location.display, MAX_LOCATION_LENGTH)
    )

    # US eligibility. Decided on the location the portal published, before any
    # scoring, deduplication or quota accounting sees the listing, and decided
    # conservatively: anything that cannot be confidently placed in the United
    # States is rejected rather than guessed at (see ``us_locations``).
    verdict = classify_us_location(location.display)
    if not verdict.is_us:
        logger.info(
            "job_rejected_non_us",
            portal=portal_slug,
            external_id=raw.external_id,
            location=location.display,
            confidence=verdict.confidence.value,
            detail=verdict.reason,
        )
        return RejectedJob(
            portal_slug=portal_slug,
            external_id=raw.external_id,
            title=title.display,
            reason=RejectionReason.NON_US_LOCATION,
        )

    # Phase 13, and deliberately here: after the US gate and before anything
    # expensive. A quick-apply listing must never be scored, and it must never
    # be canonicalised either - otherwise it could claim the canonical key of a
    # real vacancy and take its place in the pool.
    application = classify_application_type(raw)
    if not application.is_eligible:
        logger.info(
            "job_rejected_quick_apply",
            portal=portal_slug,
            external_id=raw.external_id,
            title=title.display,
            detail=application.reason,
            evidence=application.evidence,
        )
        return RejectedJob(
            portal_slug=portal_slug,
            external_id=raw.external_id,
            title=title.display,
            reason=RejectionReason.QUICK_APPLY,
        )

    # Work mode is read from the location first, then the title, then the
    # description. All three are the portal's own words; none of it is inferred
    # from a listing that says nothing, which stays None.
    work_mode = (
        location.work_mode
        or title.work_mode_hint
        or detect_work_mode(description[:2000] if description else None)
    )

    return NormalizedJob(
        portal_slug=portal_slug,
        external_id=clean_display_text(raw.external_id),
        title=title,
        company=company,
        location=location,
        work_mode=work_mode,
        employment_type=normalize_employment_type(raw.employment_type),
        description=description,
        description_fingerprint=fingerprint(description),
        source_url=source_url,
        application_url=application_url,
        posted_at=normalize_posted_at(raw.posted_at, now=now),
        canonical_key=build_canonical_key(
            company_key=company.key if company else "",
            title_key=title.key,
            location_key=location.key,
            level=title.level,
            fallback=source_url.canonical,
        ),
    )


def url_kind_for_storage(url: NormalizedUrl | None) -> JobUrlKind:
    """The kind to store on a source row, defaulting honestly to UNKNOWN."""
    return url.kind if url is not None else JobUrlKind.UNKNOWN
