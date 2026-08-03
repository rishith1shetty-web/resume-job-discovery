"""Fair per-portal quotas (CLAUDE.md section 5).

Pure functions, no database and no portal knowledge, so the rule can be tested
directly and reused by the Phase 7 orchestrator without dragging a session
along.

Quotas apply to *qualified* jobs. Nothing here licenses including a weak job to
close a gap - an unfilled quota stays unfilled and is redistributed instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def compute_quotas(requested_limit: int, portal_count: int) -> list[int]:
    """Split ``requested_limit`` as evenly as possible over the portals.

    The remainder goes to the first portals, one each, so 20 over 3 portals is
    ``[7, 7, 6]`` rather than ``[6, 6, 8]``.

        >>> compute_quotas(20, 2)
        [10, 10]
        >>> compute_quotas(50, 4)
        [13, 13, 12, 12]
        >>> compute_quotas(100, 8)
        [13, 13, 13, 13, 12, 12, 12, 12]
    """
    if portal_count <= 0:
        raise ValueError("portal_count must be at least 1.")
    if requested_limit < 0:
        raise ValueError("requested_limit must not be negative.")

    base, remainder = divmod(requested_limit, portal_count)
    return [base + (1 if index < remainder else 0) for index in range(portal_count)]


@dataclass(frozen=True, slots=True)
class Redistribution:
    """The outcome of moving unused slots between portals.

    ``shortfalls`` records what could not be filled at all, so the search
    summary can say so plainly instead of quietly returning fewer jobs.
    """

    final_quotas: list[int]
    redistributed: int
    shortfalls: list[int]


def redistribute(
    quotas: Sequence[int],
    qualified_available: Sequence[int],
) -> Redistribution:
    """Move slots a portal cannot fill to portals that have more qualified jobs.

    ``qualified_available`` is how many *qualified* jobs each portal actually
    produced. Only genuine shortages are redistributed, and only to portals
    with spare qualified jobs - the total never grows beyond what qualified.
    """
    if len(quotas) != len(qualified_available):
        raise ValueError("quotas and qualified_available must be the same length.")

    pairs = list(zip(quotas, qualified_available, strict=True))

    # What each portal can fill from its own qualified jobs, and what it is
    # short by. Shortfalls are recorded before redistribution: they describe
    # this portal's own gap, which is what the run summary needs to report.
    final = [min(quota, available) for quota, available in pairs]
    shortfalls = [quota - filled for (quota, _), filled in zip(pairs, final, strict=True)]

    spare = sum(shortfalls)
    redistributed = 0

    # Repeated passes, one slot at a time: a single pass would hand every spare
    # slot to the first portal with headroom instead of spreading them.
    while spare > 0:
        progressed = False
        for index, (_, available) in enumerate(pairs):
            if spare == 0:
                break
            if final[index] < available:
                final[index] += 1
                spare -= 1
                redistributed += 1
                progressed = True
        if not progressed:
            break

    return Redistribution(
        final_quotas=final, redistributed=redistributed, shortfalls=shortfalls
    )
