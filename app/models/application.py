"""Manual application tracking.

The user applies through the URL themselves and records what happened. Nothing
in this module submits anything, and no auto-apply concept appears in it.

Two tables:

``applications``                current status, one row per
                                (user, resume, canonical job).
``application_status_history``  append-only record of every transition.

Two rules this schema enforces rather than trusts code to remember:

* A job going stale cannot touch an application. Expiry lives on
  ``jobs.expired_at``; ``EXPIRED`` is deliberately not a status value.
* Rediscovering the same vacancy through another portal cannot reset it. The
  unique key is the *canonical* job, so a second source resolves to the same
  application row (docs/DECISIONS.md D-003).
"""

from __future__ import annotations

# Imported at runtime: SQLAlchemy evaluates `Mapped[uuid.UUID]` annotations in
# the module namespace.
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ApplicationStatus
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.resume import Resume
    from app.models.user import User

#: Statuses that mean the user has actually submitted an application. Reaching
#: any of them stamps ``applied_at``.
APPLIED_STATUSES: frozenset[ApplicationStatus] = frozenset(
    {
        ApplicationStatus.APPLIED,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
    }
)

_APPLIED_SQL = ", ".join(f"'{status.value}'" for status in sorted(APPLIED_STATUSES))


def _utcnow() -> datetime:
    """Wall-clock now, which advances inside a transaction unlike SQL now()."""
    return datetime.now(UTC)


class Application(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user's manual application state for one canonical job.

    Scoped by user *and* resume: applying with one resume must not mark the
    same job applied for another (CLAUDE.md section 11).
    """

    __tablename__ = "applications"
    __table_args__ = (
        # The resume-wise scoping rule, as a database constraint.
        UniqueConstraint("user_id", "resume_id", "job_id"),
        # A submitted application always knows when it was submitted.
        CheckConstraint(
            f"status NOT IN ({_APPLIED_SQL}) OR applied_at IS NOT NULL",
            name="applied_at_required_when_submitted",
        ),
        Index("ix_applications_resume_id_status", "resume_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[ApplicationStatus] = mapped_column(
        enum_column(ApplicationStatus, "application_status"),
        default=ApplicationStatus.NOT_APPLIED,
        nullable=False,
        index=True,
    )
    # Set when the user marks the application submitted; never cleared by the
    # system, so a later status change keeps the original submission time.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(lazy="raise")
    resume: Mapped[Resume] = relationship(lazy="raise")
    job: Mapped[Job] = relationship(lazy="raise")
    history: Mapped[list[ApplicationStatusHistory]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ApplicationStatusHistory.changed_at",
        lazy="raise",
    )

    @property
    def is_submitted(self) -> bool:
        return self.status in APPLIED_STATUSES

    def __repr__(self) -> str:
        return (
            f"<Application resume={self.resume_id} job={self.job_id} "
            f"status={self.status}>"
        )


class ApplicationStatusHistory(UUIDPrimaryKeyMixin, Base):
    """One status transition. Append-only.

    No ``updated_at``: rows are written once and never edited. A repeated
    identical update writes nothing at all, which is why ``old_status`` and
    ``new_status`` are constrained to differ.
    """

    __tablename__ = "application_status_history"
    __table_args__ = (
        # Rejects a no-op transition at the database level, so duplicate
        # history rows cannot appear even through a direct write.
        CheckConstraint(
            "old_status IS DISTINCT FROM new_status", name="status_actually_changed"
        ),
        Index(
            "ix_application_status_history_application_id_changed_at",
            "application_id",
            "changed_at",
        ),
    )

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Null on the first row: the application did not exist before it.
    # The two enum constraints need distinct names - a table cannot carry two
    # CHECK constraints called the same thing.
    old_status: Mapped[ApplicationStatus | None] = mapped_column(
        enum_column(ApplicationStatus, "application_status_old")
    )
    new_status: Mapped[ApplicationStatus] = mapped_column(
        enum_column(ApplicationStatus, "application_status_new"), nullable=False
    )
    # Client-side default as well as the server one: PostgreSQL's now() is the
    # *transaction* timestamp, so two transitions written inside one
    # transaction would tie and the history could not be ordered. The server
    # default remains for rows inserted by direct SQL.
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )
    # Who or what made the change. "user" today; a future importer would be
    # named here rather than pretending to be the user.
    changed_by: Mapped[str] = mapped_column(
        String(32), default="user", server_default="user", nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text)

    application: Mapped[Application] = relationship(
        back_populates="history", lazy="raise"
    )

    def __repr__(self) -> str:
        return (
            f"<ApplicationStatusHistory {self.old_status} -> {self.new_status} "
            f"at={self.changed_at}>"
        )
