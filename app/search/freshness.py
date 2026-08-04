"""What "recent" means, numerically.

CLAUDE.md section 6: a portal is searched newest-first and the window widens
only when the current one has not produced enough *qualified* jobs. Age is
never a rejection reason on its own - a one-week-old 94% match outranks a
five-minute-old 71% match (section 8).

Two jobs this module does:

* decide whether a listing belongs to a window (``within_window``)
* score recency for the stored ranking blend (``freshness_score``)

# Undated listings

A portal that does not publish a posting date leaves ``posted_at`` as None, and
nothing here invents one. An undated listing is admitted only at
``OLDER_ACTIVE``, the final window: it is still a real, active vacancy, so
discarding it would lose genuine results, but it cannot be claimed to be fresh.
For the same reason its freshness score is 0 rather than a flattering guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.core.constants import FRESHNESS_ESCALATION, FreshnessWindow

#: Maximum age a listing may have to belong to each window. ``None`` means no
#: upper bound: OLDER_ACTIVE accepts anything still active, which is what makes
#: it the final fallback rather than another filter.
WINDOW_MAX_AGE: dict[FreshnessWindow, timedelta | None] = {
    FreshnessWindow.LAST_24H: timedelta(days=1),
    FreshnessWindow.LAST_3D: timedelta(days=3),
    FreshnessWindow.LAST_7D: timedelta(days=7),
    FreshnessWindow.LAST_14D: timedelta(days=14),
    FreshnessWindow.LAST_30D: timedelta(days=30),
    FreshnessWindow.OLDER_ACTIVE: None,
}

#: Age at which freshness scores zero. Matches the widest dated window, so the
#: score and the escalation ladder describe the same notion of recent.
FRESHNESS_HORIZON = timedelta(days=30)


def cutoff_for(window: FreshnessWindow, now: datetime) -> datetime | None:
    """Oldest ``posted_at`` that still belongs to ``window``.

    ``None`` for OLDER_ACTIVE, which has no lower bound.
    """
    max_age = WINDOW_MAX_AGE[window]
    return None if max_age is None else now - max_age


def within_window(
    posted_at: datetime | None, window: FreshnessWindow, now: datetime
) -> bool:
    """Whether a listing belongs to this window.

    A listing posted in the future - portals occasionally publish one - is
    treated as current rather than filtered out.
    """
    if window is FreshnessWindow.OLDER_ACTIVE:
        return True
    if posted_at is None:
        # Undated listings wait for OLDER_ACTIVE rather than being guessed into
        # a window they may not belong to.
        return False

    cutoff = cutoff_for(window, now)
    assert cutoff is not None  # only OLDER_ACTIVE has no cutoff
    return posted_at >= cutoff


def windows_from(deepest: FreshnessWindow) -> tuple[FreshnessWindow, ...]:
    """The escalation ladder up to and including ``deepest``."""
    index = FRESHNESS_ESCALATION.index(deepest)
    return FRESHNESS_ESCALATION[: index + 1]


def freshness_score(posted_at: datetime | None, now: datetime) -> float:
    """Recency as 0-100, decaying linearly to zero at the horizon.

    Used only for the stored ``ranking_score`` blend. The returned result order
    is decided by the explicit tie-break chain in ``allocation``, so this never
    silently reorders anything.
    """
    if posted_at is None:
        return 0.0

    age = now - posted_at
    if age <= timedelta(0):
        return 100.0
    if age >= FRESHNESS_HORIZON:
        return 0.0

    remaining = 1.0 - (age / FRESHNESS_HORIZON)
    return round(remaining * 100.0, 4)


__all__ = [
    "FRESHNESS_HORIZON",
    "WINDOW_MAX_AGE",
    "cutoff_for",
    "freshness_score",
    "windows_from",
    "within_window",
]
