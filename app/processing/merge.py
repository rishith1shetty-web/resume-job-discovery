"""Choosing which value survives when several portals describe one vacancy.

One rule, applied per field: **a new source may only improve a field, never
degrade it** (Phase 5 brief). Every function here answers "is the incoming
value better than the one already stored?" and defaults to no.

The comparisons are deterministic and order-independent where it matters, so
processing the same sources in a different order reaches the same canonical
job. Field provenance is not stored on ``jobs`` - it does not need to be.
Every contributing capture is kept verbatim in ``raw_jobs`` with its ``job_id``
set, and every portal's own URLs and identifiers in ``job_sources``, so which
portal supplied which value is recoverable without another column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.core.constants import WorkMode
from app.processing.locations import NormalizedLocation
from app.processing.normalize import NormalizedJob

# A title cut short by a portal's column width. Keeping the truncated form when
# a complete one is available loses real words.
_TRUNCATED = re.compile(r"(?:\.{3}|…)\s*$")
_DIGIT_RUN = re.compile(r"\d{4,}")


@dataclass(slots=True)
class MergeOutcome:
    """What a merge changed, and which portal supplied each new value."""

    changed: dict[str, object] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        return bool(self.changed)

    def record(self, field_name: str, value: object, portal_slug: str) -> None:
        self.changed[field_name] = value
        self.provenance[field_name] = portal_slug


def title_quality(title: str | None) -> int:
    """Rough display quality of a title. Higher is better.

    Only used to decide whether an incoming title is worth adopting; it never
    rewrites a title, so a wrong answer costs display polish and nothing else.
    """
    if not title:
        return -1

    score = 100

    if _TRUNCATED.search(title):
        score -= 50
    letters = [ch for ch in title if ch.isalpha()]
    if letters:
        # SHOUTED and lowercased titles both read badly in a results table.
        if all(ch.isupper() for ch in letters):
            score -= 20
        elif all(ch.islower() for ch in letters):
            score -= 10
    if _DIGIT_RUN.search(title):
        score -= 15
    # Very short titles are usually a fragment rather than the role.
    if len(title) < 6:
        score -= 25

    return score


def better_title(existing: str | None, incoming: str | None) -> str | None:
    """Keep the existing title unless the incoming one is measurably better."""
    if not existing:
        return incoming
    if not incoming:
        return existing

    if title_quality(incoming) > title_quality(existing):
        return incoming
    return existing


def better_company(existing: str | None, incoming: str | None) -> str | None:
    """Prefer a present company, then the more complete spelling.

    Only called once the two names have already been judged the same employer,
    so preferring the longer form picks up "Acme Financial Services" over a
    bare "Acme" without risking a merge between different companies.
    """
    if not existing:
        return incoming
    if not incoming:
        return existing

    return incoming if len(incoming) > len(existing) else existing


def better_location(
    existing: NormalizedLocation | None, incoming: NormalizedLocation | None
) -> NormalizedLocation | None:
    """Prefer the more specific location, never a conflicting one.

    "Boston" is replaced by "Boston, MA" because the second says everything the
    first does and more. "Boston" is not replaced by "Denver": that is a
    different place, and by the time a merge happens the two have already been
    judged compatible, so the safe move is to keep what is stored.
    """
    if existing is None or not existing.parts:
        return incoming or existing
    if incoming is None or not incoming.parts:
        return existing

    existing_parts, incoming_parts = set(existing.parts), set(incoming.parts)
    if existing_parts < incoming_parts:
        return incoming

    return existing


def better_description(existing: str | None, incoming: str | None) -> str | None:
    """Prefer the fuller description.

    Length is a crude proxy, but a reliable one here: portals truncate, they do
    not pad. A 4,000-character posting and a 200-character teaser of the same
    job are not equally useful to Phase 6, which refuses to score a job it
    cannot read.
    """
    if not existing:
        return incoming
    if not incoming:
        return existing

    return incoming if len(incoming) > len(existing) else existing


def earliest_posted_at(
    existing: datetime | None, incoming: datetime | None
) -> datetime | None:
    """Keep the earliest known posting date.

    Portals report when *they* discovered a job, so the earliest date across
    sources is the closest thing to when the employer actually published it.
    Taking the latest would make a job look newer every time another
    aggregator picked it up, which would quietly game the freshness ranking.
    """
    if existing is None:
        return incoming
    if incoming is None:
        return existing

    return min(existing, incoming)


def better_work_mode(
    existing: WorkMode | None, incoming: WorkMode | None
) -> WorkMode | None:
    """Fill in an unknown work mode; never overwrite a stated one.

    Two portals disagreeing means one of them read it wrong, and there is no
    way to tell which. The stored value stays.
    """
    return existing if existing is not None else incoming


def merge_into_existing(
    *,
    existing_title: str | None,
    existing_company: str | None,
    existing_location: NormalizedLocation | None,
    existing_work_mode: WorkMode | None,
    existing_employment_type: str | None,
    existing_description: str | None,
    existing_posted_at: datetime | None,
    incoming: NormalizedJob,
) -> MergeOutcome:
    """Compute the field changes an incoming source justifies.

    Returns only what should actually change, so a re-run of the same source
    produces an empty outcome and the caller can report processing as
    idempotent rather than guessing.
    """
    outcome = MergeOutcome()
    portal = incoming.portal_slug

    title = better_title(existing_title, incoming.title.display)
    if title != existing_title:
        outcome.record("title", title, portal)

    company_display = incoming.company.display if incoming.company else None
    company = better_company(existing_company, company_display)
    if company != existing_company:
        outcome.record("company", company, portal)

    location = better_location(existing_location, incoming.location)
    existing_display = existing_location.display if existing_location else None
    if location is not None and location.display != existing_display:
        outcome.record("location", location.display, portal)

    work_mode = better_work_mode(existing_work_mode, incoming.work_mode)
    if work_mode != existing_work_mode:
        outcome.record("work_mode", work_mode, portal)

    if not existing_employment_type and incoming.employment_type:
        outcome.record("employment_type", incoming.employment_type, portal)

    description = better_description(existing_description, incoming.description)
    if description != existing_description:
        outcome.record("description", description, portal)

    posted_at = earliest_posted_at(existing_posted_at, incoming.posted_at)
    if posted_at != existing_posted_at:
        outcome.record("posted_at", posted_at, portal)

    return outcome
