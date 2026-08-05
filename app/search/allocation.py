"""Fair per-portal shares, shortage redistribution and the final order.

CLAUDE.md section 5 and the Phase 7 brief. Pure functions over plain values:
no database, no portal names, no scoring. The quota arithmetic itself lives in
``app.services.quota`` and is reused here rather than restated.

Three rules this module exists to keep:

* **One canonical job takes one slot.** Candidates are keyed by canonical job
  id and attributed to exactly one portal - the first that surfaced them in
  this run - so a vacancy found through three portals cannot consume three
  slots (CLAUDE.md section 9).
* **A quota is filled from qualified jobs only.** An unfilled quota stays
  unfilled and is redistributed. Nothing here can add a job below the
  threshold; only already-qualified candidates are passed in.
* **The order is total.** The tie-break chain ends on the canonical job id, so
  no two candidates can compare equal and database load order cannot leak into
  the result.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.matching.roles import RoleVerdict
from app.services.quota import redistribute

#: Stronger role compatibility first. Only used as a tie-break: an incompatible
#: role is rejected long before allocation, so INCOMPATIBLE never appears here.
ROLE_RANK: dict[RoleVerdict, int] = {
    RoleVerdict.EXACT: 0,
    RoleVerdict.VARIATION: 1,
    RoleVerdict.STRONG_VARIANT: 2,
    RoleVerdict.WEAK_VARIANT: 3,
    RoleVerdict.INCOMPATIBLE: 4,
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One qualified, eligible canonical job, ready to be allocated a slot."""

    job_id: uuid.UUID
    #: Index into the run's selected-portal list. The portal that first
    #: surfaced this canonical job owns it for allocation purposes.
    portal_index: int
    final_score: float
    posted_at: datetime | None
    role_verdict: RoleVerdict
    #: 0.0-1.0 coverage of the posting's required skills.
    skill_coverage: float
    canonical_key: str


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """Who got how many slots, and what could not be filled."""

    selected: tuple[Candidate, ...]
    per_portal_available: tuple[int, ...]
    per_portal_selected: tuple[int, ...]
    target_quotas: tuple[int, ...]
    final_quotas: tuple[int, ...]
    #: Slots a portal could not fill from its own qualified jobs, before any
    #: redistribution. This is what the run summary reports as a shortage.
    shortfalls: tuple[int, ...]
    redistributed: int

    @property
    def returned_count(self) -> int:
        return len(self.selected)

    @property
    def total_shortfall(self) -> int:
        return sum(self.shortfalls)


def sort_key(candidate: Candidate) -> tuple:
    """The deterministic result order (Phase 7 brief, "Final ordering").

    In order: match score, then posting recency, then role compatibility, then
    required-skill coverage, then the canonical key, then the job id. The last
    two make the order total - without them two identically-scored jobs would
    fall back on whatever order the database returned.

    Undated listings sort after dated ones *within the same score*, never
    across scores: freshness prioritises, it does not override relevance
    (CLAUDE.md section 8).
    """
    dated = candidate.posted_at is not None

    return (
        -candidate.final_score,
        0 if dated else 1,
        -candidate.posted_at.timestamp() if dated else 0.0,
        ROLE_RANK.get(candidate.role_verdict, len(ROLE_RANK)),
        -candidate.skill_coverage,
        candidate.canonical_key,
        str(candidate.job_id),
    )


def order_candidates(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Sort into the final presentation order."""
    return sorted(candidates, key=sort_key)


@dataclass(frozen=True, slots=True)
class GlobalSelection:
    """The Top N of one combined candidate pool (Phase 12).

    Replaces per-portal quotas as the way a run decides what to return. Every
    qualified job from every portal competes in one pool on the same ranking,
    so a portal that found thirty good jobs contributes thirty if they are the
    best thirty - and a portal that found two contributes two, without holding
    slots that it cannot fill.
    """

    selected: tuple[Candidate, ...]
    #: Qualified candidates each portal contributed to the pool, by index.
    per_portal_available: tuple[int, ...]
    #: How many of each portal's candidates survived into the Top N.
    per_portal_selected: tuple[int, ...]
    pool_size: int

    @property
    def returned_count(self) -> int:
        return len(self.selected)


def select_global_top(
    candidates: Sequence[Candidate],
    *,
    portal_count: int,
    requested_limit: int,
) -> GlobalSelection:
    """Rank the whole pool together and take the best ``requested_limit``.

    The ranking itself is unchanged: this calls the same ``order_candidates``
    the quota engine did. What changed is only *when* jobs reach it - all of
    them, from every portal, at once, rather than the survivors of a per-portal
    cut made before the comparison.

    Passing the same candidates in a different order returns the same result:
    ``sort_key`` ends on the canonical key and the job id, so the order is
    total and no database load order can leak in.
    """
    if portal_count < 0:
        raise ValueError("portal_count cannot be negative.")

    ordered = order_candidates(candidates)
    selected = ordered[: max(requested_limit, 0)]

    available = [0] * portal_count
    for candidate in ordered:
        if 0 <= candidate.portal_index < portal_count:
            available[candidate.portal_index] += 1

    taken = [0] * portal_count
    for candidate in selected:
        if 0 <= candidate.portal_index < portal_count:
            taken[candidate.portal_index] += 1

    return GlobalSelection(
        selected=tuple(selected),
        per_portal_available=tuple(available),
        per_portal_selected=tuple(taken),
        pool_size=len(ordered),
    )


def allocate(
    candidates: Sequence[Candidate],
    *,
    quotas: Sequence[int],
    requested_limit: int,
) -> AllocationResult:
    """Give each portal an approximately equal share of the qualified jobs.

    ``quotas`` is the target share per selected portal, positionally aligned
    with ``Candidate.portal_index``. A portal short of qualified jobs releases
    its unused slots to portals that have more; the total never exceeds
    ``requested_limit`` and never includes an unqualified job.

    Passing the same candidates in a different order returns the same result:
    every bucket is ordered before anything is taken.
    """
    portal_count = len(quotas)
    if portal_count == 0:
        raise ValueError("allocate needs at least one portal quota.")

    # Bucket by owning portal, each bucket in presentation order.
    buckets: list[list[Candidate]] = [[] for _ in range(portal_count)]
    for candidate in order_candidates(candidates):
        if 0 <= candidate.portal_index < portal_count:
            buckets[candidate.portal_index].append(candidate)

    available = [len(bucket) for bucket in buckets]
    outcome = redistribute(quotas, available)

    taken: list[Candidate] = []
    per_portal_selected: list[int] = []
    for bucket, quota in zip(buckets, outcome.final_quotas, strict=True):
        share = bucket[:quota]
        per_portal_selected.append(len(share))
        taken.extend(share)

    # Belt and braces: redistribute never grows the total beyond sum(quotas),
    # and sum(quotas) is the requested limit. Truncating here means a future
    # change to either cannot quietly return more than the user asked for.
    ordered = order_candidates(taken)[:requested_limit]

    if len(ordered) != len(taken):
        # Recount: truncation may have dropped a portal's last pick.
        recount = [0] * portal_count
        for candidate in ordered:
            recount[candidate.portal_index] += 1
        per_portal_selected = recount

    return AllocationResult(
        selected=tuple(ordered),
        per_portal_available=tuple(available),
        per_portal_selected=tuple(per_portal_selected),
        target_quotas=tuple(quotas),
        final_quotas=tuple(outcome.final_quotas),
        shortfalls=tuple(outcome.shortfalls),
        redistributed=outcome.redistributed,
    )


__all__ = [
    "ROLE_RANK",
    "AllocationResult",
    "Candidate",
    "GlobalSelection",
    "allocate",
    "order_candidates",
    "select_global_top",
    "sort_key",
]
