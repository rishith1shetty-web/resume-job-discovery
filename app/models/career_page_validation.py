"""What fetching a company's career page established, and when.

A cache, and only a cache. Nothing here is a result, nothing here is shown to
a user directly, and losing the whole table costs a run some HTTP requests and
nothing else.

# Why it exists

Validation fetches. A search that turns up eighty candidate employers would
otherwise open eighty connections to eighty employers' sites every time it
runs, including for the twenty companies whose page was checked an hour ago
and has not changed. That is slow for the user and discourteous to the
employers, so a verdict earned once is reused for a while.

# Why it expires, and why failure expires sooner

A career page is not a stable object. A board that listed forty roles today can
be closed, moved behind a login, or 404 next month, so a verdict that never
expired would eventually be a claim about the past presented as the present.
Hence ``last_validated_at`` and a time-to-live rather than a permanent record.

Failures expire faster than successes, and deliberately so: the brief is
explicit that a cached failure must not permanently eliminate a company. A
site that declined our request this morning may answer this afternoon, and an
employer must be able to come back. So ``UNAVAILABLE`` - which is a statement
about *our request*, not about the page - is held only briefly, while
``REJECTED`` (a login wall, a 404, the vendor's lobby) is held longer because
re-asking sooner would almost certainly get the same answer.

# Why it is keyed the way it is

On ``career_page_key`` from ``app.careers.identity`` - the employer plus the
host their careers presence lives on - which is the same identity a user's
Applied/Skipped decision is stored against. Two runs that reach one employer
through two different postings share one cache row, which is the entire point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.careers.identity import CAREER_PAGE_KEY_LENGTH
from app.core.constants import CareerPageStatus
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column

#: How long a proven page is trusted before it is checked again. Long enough
#: that repeat searches in a working session cost nothing, short enough that a
#: board taken down today is not still being offered next week.
VERIFIED_TTL = timedelta(hours=12)

#: How long a page proven *bad* is remembered. Longer than a verified one
#: because a 404 or a Company ID wall does not repair itself in an afternoon,
#: but still finite, because employers do fix their careers sites.
REJECTED_TTL = timedelta(days=3)

#: How long a "we could not look" verdict is remembered. Deliberately short:
#: this is the state a temporary block, a timeout or a robots read failure
#: lands in, and none of those is a property of the employer. A company must be
#: able to reappear on the next search.
UNAVAILABLE_TTL = timedelta(minutes=30)

_TTL_BY_STATUS: dict[CareerPageStatus, timedelta] = {
    CareerPageStatus.VERIFIED: VERIFIED_TTL,
    CareerPageStatus.REJECTED: REJECTED_TTL,
    CareerPageStatus.UNVERIFIED: REJECTED_TTL,
    CareerPageStatus.UNAVAILABLE: UNAVAILABLE_TTL,
}


class CareerPageValidation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One cached verdict about one company's career page."""

    __tablename__ = "career_page_validations"
    __table_args__ = (
        Index(
            "ix_career_page_validations_key",
            "career_page_key",
            unique=True,
        ),
        Index("ix_career_page_validations_status", "status"),
    )

    #: Stable identity from ``app.careers.identity``: employer plus careers
    #: host, hashed. The same key a user's decision about the page uses.
    career_page_key: Mapped[str] = mapped_column(
        String(CAREER_PAGE_KEY_LENGTH), nullable=False
    )

    #: Snapshot of what was checked, for operators reading the table directly.
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    #: The URL that was requested, before redirects.
    candidate_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: Where the redirect chain ended and the verdict was formed. Null unless
    #: the page verified - an unusable destination is not worth remembering as
    #: somewhere to send anyone.
    verified_url: Mapped[str | None] = mapped_column(String(2048))
    #: ``JobUrlKind`` as a string, kept loose because it is display metadata
    #: here rather than a constrained domain value.
    url_kind: Mapped[str | None] = mapped_column(String(40))

    status: Mapped[CareerPageStatus] = mapped_column(
        enum_column(CareerPageStatus, "career_page_status"),
        nullable=False,
    )
    validation_reason: Mapped[str | None] = mapped_column(Text)
    last_validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def ttl(self) -> timedelta:
        return _TTL_BY_STATUS.get(self.status, UNAVAILABLE_TTL)

    def is_fresh(self, *, now: datetime | None = None) -> bool:
        """Is this verdict still worth reusing instead of re-fetching?

        A row whose status has no place in the cache - ``DISCOVERED`` or
        ``VALIDATING``, neither of which is a conclusion - is never fresh, so a
        half-written row cannot be mistaken for an answer.
        """
        if self.status not in _TTL_BY_STATUS:
            return False
        moment = now or datetime.now(UTC)
        checked = self.last_validated_at
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        return moment - checked < self.ttl()

    def __repr__(self) -> str:
        return (
            f"<CareerPageValidation {self.company_name!r} "
            f"status={self.status} at={self.last_validated_at}>"
        )


__all__ = [
    "REJECTED_TTL",
    "UNAVAILABLE_TTL",
    "VERIFIED_TTL",
    "CareerPageValidation",
]
