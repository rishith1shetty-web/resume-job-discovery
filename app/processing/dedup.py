"""Deciding whether two listings are the same vacancy.

Never on one field alone (CLAUDE.md section 9). A URL match is strong, a title
match is not, and the rules below say which combinations are enough.

The asymmetry that drives every threshold here: a **false merge** hides a real
vacancy behind another one and can show the user an "Applied" status for a job
they never applied to, while a **missed merge** shows the same job twice, which
they can see for themselves. So every ambiguous case resolves to "not a
duplicate", and every decision carries the reason that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from app.core.constants import JobUrlKind
from app.processing.companies import companies_match
from app.processing.locations import NormalizedLocation, locations_compatible
from app.processing.normalize import NormalizedJob
from app.processing.titles import NormalizedTitle, title_equivalence

#: Postings further apart than this are treated as separate advertisements when
#: the evidence is only "probable". Portals republish for weeks, so the window
#: is generous; it only ever has to stop a spring and an autumn opening from
#: merging on title alone.
PROBABLE_DUPLICATE_WINDOW = timedelta(days=45)


class DuplicateKind(StrEnum):
    """How certain a duplicate verdict is."""

    #: Identity proven by a shared identifier or application URL.
    HARD = "hard"
    #: Employer, role and place all agree, but nothing proves identity.
    PROBABLE = "probable"
    #: Not the same vacancy, or not enough evidence to say so.
    NONE = "none"


@dataclass(frozen=True, slots=True)
class DuplicateVerdict:
    """The outcome of comparing two listings, with its evidence."""

    kind: DuplicateKind
    confidence: float
    signals: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def is_duplicate(self) -> bool:
        return self.kind is not DuplicateKind.NONE

    def as_dict(self) -> dict[str, object]:
        """Serialisable form, for logs and the processing preview endpoint."""
        return {
            "kind": self.kind.value,
            "confidence": round(self.confidence, 3),
            "signals": list(self.signals),
            "blockers": list(self.blockers),
        }


NOT_DUPLICATE = DuplicateVerdict(kind=DuplicateKind.NONE, confidence=0.0)


@dataclass(frozen=True, slots=True)
class ExistingJobView:
    """The normalised facts about an already-stored job.

    A plain value object rather than a SQLAlchemy model, so the matching rules
    stay testable without a database and the service layer decides how to load
    them.
    """

    job_id: object
    canonical_key: str
    title: NormalizedTitle
    company_key: str
    company_display: str | None
    location: NormalizedLocation
    posted_at: object | None = None
    description_fingerprint: str | None = None
    #: Canonical URLs of every source already attached to this job.
    source_canonical_urls: frozenset[str] = field(default_factory=frozenset)
    #: Canonical URLs that are an ATS or verified employer destination.
    application_canonical_urls: frozenset[str] = field(default_factory=frozenset)
    #: (portal_slug, external_id) pairs already recorded.
    external_ids: frozenset[tuple[str, str]] = field(default_factory=frozenset)


def _dates_within_window(left: object, right: object) -> tuple[bool, str]:
    if left is None or right is None:
        return True, "posting date unknown on one side"

    try:
        gap = abs(left - right)  # type: ignore[operator]
    except TypeError:  # pragma: no cover - defensive, both are datetimes
        return True, "posting dates not comparable"

    if gap <= PROBABLE_DUPLICATE_WINDOW:
        return True, f"posted {gap.days} days apart"

    return False, f"posted {gap.days} days apart, beyond the {PROBABLE_DUPLICATE_WINDOW.days}-day window"


def _hard_signals(
    candidate: NormalizedJob, existing: ExistingJobView
) -> tuple[str, ...]:
    """Evidence that proves identity outright."""
    signals: list[str] = []

    # Same portal, same id for that portal. The portal itself says these are
    # one listing.
    if candidate.external_id:
        pair = (candidate.portal_slug, candidate.external_id)
        if pair in existing.external_ids:
            signals.append(
                f"same external id on {candidate.portal_slug} "
                f"({candidate.external_id})"
            )

    # The same ATS or employer application URL is the requisition itself: two
    # portals linking to it are advertising one vacancy. A shared *portal*
    # listing URL is not used here - two aggregators can link to the same
    # third-party listing without it being the same requisition.
    if candidate.application_url is not None and candidate.application_url.kind in (
        JobUrlKind.ATS,
        JobUrlKind.EMPLOYER_CAREER,
    ):
        if candidate.application_url.canonical in existing.application_canonical_urls:
            signals.append(
                f"same {candidate.application_url.kind.value} application URL"
            )

    if candidate.source_url.kind in (JobUrlKind.ATS, JobUrlKind.EMPLOYER_CAREER):
        if candidate.source_url.canonical in existing.source_canonical_urls:
            signals.append(f"same {candidate.source_url.kind.value} listing URL")

    return tuple(signals)


def compare(candidate: NormalizedJob, existing: ExistingJobView) -> DuplicateVerdict:
    """Decide whether ``candidate`` is the vacancy ``existing`` already records."""
    hard = _hard_signals(candidate, existing)
    if hard:
        # Proven identity. A title or location that disagrees means one portal
        # has worse metadata, not that these are different jobs.
        return DuplicateVerdict(kind=DuplicateKind.HARD, confidence=1.0, signals=hard)

    signals: list[str] = []
    blockers: list[str] = []

    # From here on every gate must pass. Employer first: without a matching
    # employer nothing else is worth checking.
    company_ok, company_reason = companies_match(
        candidate.company,
        _as_company(existing),
    )
    if not company_ok:
        return DuplicateVerdict(
            kind=DuplicateKind.NONE, confidence=0.0, blockers=(company_reason,)
        )
    signals.append(company_reason)

    title_ok, title_score, title_reason = title_equivalence(
        candidate.title, existing.title
    )
    if not title_ok:
        return DuplicateVerdict(
            kind=DuplicateKind.NONE,
            confidence=0.0,
            signals=tuple(signals),
            blockers=(title_reason,),
        )
    signals.append(title_reason)

    location_ok, location_reason = locations_compatible(
        candidate.location, existing.location
    )
    if not location_ok:
        return DuplicateVerdict(
            kind=DuplicateKind.NONE,
            confidence=0.0,
            signals=tuple(signals),
            blockers=(location_reason,),
        )
    signals.append(location_reason)

    dates_ok, dates_reason = _dates_within_window(
        candidate.posted_at, existing.posted_at
    )
    if not dates_ok:
        return DuplicateVerdict(
            kind=DuplicateKind.NONE,
            confidence=0.0,
            signals=tuple(signals),
            blockers=(dates_reason,),
        )
    signals.append(dates_reason)

    # Identical posting copy is strong corroboration on top of the gates above.
    same_text = bool(
        candidate.description_fingerprint
        and candidate.description_fingerprint == existing.description_fingerprint
    )
    if same_text:
        signals.append("identical description text")

    confidence = min(1.0, 0.6 + 0.25 * title_score + (0.15 if same_text else 0.0))

    return DuplicateVerdict(
        kind=DuplicateKind.PROBABLE,
        confidence=confidence,
        signals=tuple(signals),
        blockers=tuple(blockers),
    )


def _as_company(existing: ExistingJobView):
    """Adapt a stored job's employer into the shape ``companies_match`` takes."""
    from app.processing.companies import NormalizedCompany

    if not existing.company_key:
        return None
    return NormalizedCompany(
        display=existing.company_display or existing.company_key,
        key=existing.company_key,
    )


def best_match(
    candidate: NormalizedJob, existing_jobs: list[ExistingJobView]
) -> tuple[ExistingJobView | None, DuplicateVerdict]:
    """Pick the strongest duplicate among candidates, if any.

    Hard beats probable, then higher confidence wins. Ties fall through to the
    canonical key and finally the job's own id, which is unique - without that
    last step two candidates with the same key would be separated only by the
    order they were loaded in, and the same input could merge differently on a
    second run.
    """

    def rank(view: ExistingJobView, verdict: DuplicateVerdict) -> tuple:
        return (
            verdict.kind is DuplicateKind.HARD,
            verdict.confidence,
            view.canonical_key,
            str(view.job_id),
        )

    best: tuple[ExistingJobView, DuplicateVerdict] | None = None

    for existing in existing_jobs:
        verdict = compare(candidate, existing)
        if not verdict.is_duplicate:
            continue

        if best is None or rank(existing, verdict) > rank(best[0], best[1]):
            best = (existing, verdict)

    if best is None:
        return None, NOT_DUPLICATE

    return best
