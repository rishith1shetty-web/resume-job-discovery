"""What "recent" means, numerically.

CLAUDE.md section 6: a portal is searched newest-first and the window widens
only when the current one has not produced enough *qualified* jobs.

Age **is** a rejection reason on its own. A listing posted more than
``MAX_ELIGIBLE_AGE`` ago is ineligible at any score - a 99% match posted 31 days
ago is excluded, while a 75% match posted an hour ago is kept and ranks first.
Recency is the primary sort and match quality only breaks ties (section 8).

Three jobs this module does:

* decide whether a listing is eligible at all (``is_eligible_age``)
* decide whether a listing belongs to a window (``within_window``)
* score recency for the stored ranking blend (``freshness_score``)

# Undated listings

A portal that does not publish a posting date leaves ``posted_at`` as None, and
nothing here invents one. An undated listing cannot be *shown* to be within the
eligible window, so it is excluded rather than assumed recent. The same applies
to a date that could not be parsed: the adapter leaves it None, and None is not
treated as fresh.
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

#: Hard eligibility bound. A listing older than this is rejected whatever it
#: scores. Equal to the horizon on purpose: "stops counting as fresh" and
#: "stops being eligible" are the same moment, so there is no band in which a
#: job is eligible but scores zero for recency.
MAX_ELIGIBLE_AGE = FRESHNESS_HORIZON


def is_eligible_age(posted_at: datetime | None, now: datetime) -> bool:
    """Whether a listing is recent enough to be shown at all.

    The gate is deliberately independent of the escalation ladder and of the
    score: every consumer asks this one question and gets the same answer.

    An unknown, missing or unparseable date arrives here as None and is
    rejected. It cannot be demonstrated to be within the window, and treating
    it as recent would let an arbitrarily old listing through the one rule that
    exists to stop exactly that.
    """
    if posted_at is None:
        return False
    if posted_at >= now:
        # Portals occasionally publish a future date; that is current, not stale.
        return True
    return now - posted_at <= MAX_ELIGIBLE_AGE


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
    if posted_at is None:
        # Never guessed into a window. An undated listing is not eligible at
        # all, so there is no later window for it to wait for.
        return False
    if window is FreshnessWindow.OLDER_ACTIVE:
        # No longer "anything still active": the eligibility bound applies even
        # when a caller asks for the unconstrained window, so this can never be
        # a way around the 30-day rule.
        return is_eligible_age(posted_at, now)

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
    "MAX_ELIGIBLE_AGE",
    "WINDOW_MAX_AGE",
    "cutoff_for",
    "freshness_score",
    "is_eligible_age",
    "windows_from",
    "within_window",
]
