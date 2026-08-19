"""What a user decided about one company's career page.

The job equivalent is ``app.models.application``, and this deliberately mirrors
it: one row per (user, resume, career page), a status the user sets, and
resume-wise scoping enforced by a unique constraint rather than trusted to
code.

# Why this is not a row in ``applications``

``applications.job_id`` is ``NOT NULL`` and references ``jobs.id``, inside the
unique key ``(user_id, resume_id, job_id)``. A career page has no ``jobs`` row -
it is a company's recruiting site, not a vacancy - so storing one there would
mean making ``job_id`` nullable and giving up the guarantee that every
application points at a real canonical job. That guarantee is worth more than
the saved table.

Keeping them apart also keeps a promise the product makes out loud: applying to
a career page is **not** applying to the jobs on it. Nothing here writes an
``applications`` row, and no job's status is touched when a career page is
marked.

# Why the display fields are stored here

Career pages are transient discovery output; no table holds them. So the fields
needed to show the decision in history - the employer's name and the URL the
user opened - are snapshotted onto the decision itself. They are a record of
what was acted on, not a cache to be refreshed.

# Mutual exclusivity

Applied and Skipped cannot both hold, because they are one column on one row.
There is no second table and no pair of booleans that could disagree.
"""

from __future__ import annotations

# Imported at runtime: SQLAlchemy evaluates `Mapped[uuid.UUID]` annotations in
# the module namespace.
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.careers.identity import CAREER_PAGE_KEY_LENGTH
from app.core.constants import ApplicationStatus
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column

if TYPE_CHECKING:
    from app.models.resume import Resume
    from app.models.user import User

#: The decisions a career page accepts.
#:
#: A deliberate subset of ``ApplicationStatus``. A career page has no interview,
#: no rejection and no offer - those are outcomes of applying to a *vacancy*,
#: and a page is a place to go and look. ``NOT_APPLIED`` is the absence of a
#: decision and is represented by having no row, not by a row saying so.
CAREER_PAGE_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {ApplicationStatus.APPLIED, ApplicationStatus.SKIPPED}
)

#: Decisions that stop a career page being offered again for that resume.
#: Both of them: the user has acted on the page either way, and showing it
#: again is the same nuisance as re-offering a job they already dealt with.
#: Mirrors ``INELIGIBLE_STATUSES`` in ``app.matching.eligibility``.
INELIGIBLE_CAREER_PAGE_STATUSES: frozenset[ApplicationStatus] = CAREER_PAGE_STATUSES

_STATUS_SQL = ", ".join(f"'{status.value}'" for status in sorted(CAREER_PAGE_STATUSES))


class CareerPageInteraction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user's decision about one career page, under one resume."""

    __tablename__ = "career_page_interactions"
    __table_args__ = (
        # The resume-wise scoping rule, as a database constraint. Resume A
        # applying to an employer's page leaves it available to Resume B.
        UniqueConstraint(
            "user_id",
            "resume_id",
            "career_page_key",
            name="uq_career_page_interactions_resume_page",
        ),
        # Applied and Skipped are the only decisions a page can hold, so a
        # contradictory or meaningless state cannot be written even directly.
        CheckConstraint(
            f"status IN ({_STATUS_SQL})", name="career_page_status_is_a_decision"
        ),
        # An applied page always knows when it was applied to; a skipped one
        # never carries a submission time it did not have.
        CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL)",
            name="career_page_applied_at_matches_status",
        ),
        Index(
            "ix_career_page_interactions_resume_id_status", "resume_id", "status"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Stable identity from ``app.careers.identity``: the canonicalised career
    #: URL, hashed. Two runs that surface the same page with different tracking
    #: parameters produce the same key and therefore one decision.
    career_page_key: Mapped[str] = mapped_column(
        String(CAREER_PAGE_KEY_LENGTH), nullable=False, index=True
    )

    # Snapshot of what was acted on. Career pages are not stored anywhere else,
    # so history reads these rather than joining to a table that does not exist.
    company_name: Mapped[str] = mapped_column(String(256), nullable=False)
    official_career_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    relevant_role: Mapped[str | None] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(256))

    status: Mapped[ApplicationStatus] = mapped_column(
        enum_column(ApplicationStatus, "application_status"),
        nullable=False,
        index=True,
    )
    #: Set when the user marks the page applied; cleared if they later move it
    #: to skipped, because the CHECK above keeps the two consistent.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(lazy="raise")
    resume: Mapped[Resume] = relationship(lazy="raise")

    @property
    def is_applied(self) -> bool:
        return self.status is ApplicationStatus.APPLIED

    @property
    def is_skipped(self) -> bool:
        return self.status is ApplicationStatus.SKIPPED

    def __repr__(self) -> str:
        return (
            f"<CareerPageInteraction resume={self.resume_id} "
            f"page={self.career_page_key} status={self.status}>"
        )


__all__ = [
    "CAREER_PAGE_STATUSES",
    "INELIGIBLE_CAREER_PAGE_STATUSES",
    "CareerPageInteraction",
]
