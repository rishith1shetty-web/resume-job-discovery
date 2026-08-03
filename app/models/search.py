"""Search-run bookkeeping.

Four tables:

``search_runs``         one search of one resume across the portals the user
                        ticked. Created in PENDING and never executed here -
                        Phase 7 owns orchestration.
``search_run_portals``  one row per selected portal, carrying that portal's
                        quota, counters, freshness reach and error. The unit of
                        failure isolation: one row can be FAILED while the run
                        completes (CLAUDE.md section 18).
``search_queries``      every query actually issued, per portal and window, so
                        a disappointing run can be explained after the fact.
``portal_errors``       operational error log, kept separate from the portal
                        row so a failure never mutates portal configuration.

Every search run belongs to exactly one resume. Nothing in this module is
reachable without going through a resume, which is what keeps results from
mixing between resumes (CLAUDE.md section 11).
"""

from __future__ import annotations

# Imported at runtime: SQLAlchemy evaluates `Mapped[uuid.UUID]` annotations in
# the module namespace.
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import (
    MIN_MATCH_SCORE_FLOOR,
    RESULT_LIMIT_OPTIONS,
    FreshnessWindow,
    PortalErrorKind,
    PortalRunStatus,
    SearchRunStatus,
    TERMINAL_SEARCH_RUN_STATUSES,
)
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column

if TYPE_CHECKING:
    from app.models.job import RawJobRecord
    from app.models.portal import Portal
    from app.models.profile import ResumeProfile
    from app.models.resume import Resume

_LIMIT_LIST = ", ".join(str(value) for value in RESULT_LIMIT_OPTIONS)


class SearchRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One search of one resume across the selected portals."""

    __tablename__ = "search_runs"
    __table_args__ = (
        # Only 20, 50 or 100 (CLAUDE.md section 5). Enforced in the database as
        # well as the API so no code path can write a third value.
        CheckConstraint(
            f"requested_limit IN ({_LIMIT_LIST})",
            name="requested_limit_allowed",
        ),
        # The threshold is locked and may only ever be raised.
        CheckConstraint(
            f"min_score >= {MIN_MATCH_SCORE_FLOOR} AND min_score <= 100",
            name="min_score_locked",
        ),
        Index("ix_search_runs_resume_id_created_at", "resume_id", "created_at"),
    )

    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Which analysis produced the role that was searched. Nullable because a
    # profile may be superseded; the snapshot columns below survive that.
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resume_profiles.id", ondelete="CASCADE")
    )

    requested_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    min_score: Mapped[int] = mapped_column(
        Integer, default=MIN_MATCH_SCORE_FLOOR, nullable=False
    )

    status: Mapped[SearchRunStatus] = mapped_column(
        enum_column(SearchRunStatus, "search_run_status"),
        default=SearchRunStatus.PENDING,
        nullable=False,
        index=True,
    )

    # Snapshot of what was searched. Copied rather than joined, so re-analysing
    # the resume later cannot rewrite the history of an earlier run.
    searched_domain: Mapped[str | None] = mapped_column(String(64))
    searched_target_role: Mapped[str | None] = mapped_column(String(64))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Run-level failure only. A single portal breaking belongs in portal_errors
    # and leaves the run COMPLETED_WITH_ERRORS.
    error_message: Mapped[str | None] = mapped_column(Text)

    # Quota shortages and redistributions (CLAUDE.md section 5). Free-form so
    # Phase 7 can record its reasoning without another migration.
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    resume: Mapped[Resume] = relationship(back_populates="search_runs", lazy="raise")
    profile: Mapped[ResumeProfile | None] = relationship(lazy="raise")
    portals: Mapped[list[SearchRunPortal]] = relationship(
        back_populates="search_run",
        cascade="all, delete-orphan",
        order_by="SearchRunPortal.created_at",
        lazy="raise",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_SEARCH_RUN_STATUSES

    def __repr__(self) -> str:
        return (
            f"<SearchRun id={self.id} resume_id={self.resume_id} "
            f"limit={self.requested_limit} status={self.status}>"
        )


class SearchRunPortal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One portal's leg of a search run.

    Counters are deliberately separate: ``raw_count`` is what the portal
    returned, ``qualified_count`` is what survived the 70% gate, and
    ``selected_count`` is what the quota actually took. A quota is filled from
    qualified jobs only - a weak job is never included to make the numbers work
    (CLAUDE.md section 5).
    """

    __tablename__ = "search_run_portals"
    __table_args__ = (
        # A portal is selected at most once per run.
        UniqueConstraint("search_run_id", "portal_id"),
        CheckConstraint("target_quota >= 0", name="target_quota_non_negative"),
        CheckConstraint("selected_count >= 0", name="selected_count_non_negative"),
        Index("ix_search_run_portals_status", "status"),
    )

    search_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    portal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Quota computed at run creation: requested_limit // portal_count, with the
    # remainder spread over the first portals.
    target_quota: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Quota after genuine shortages elsewhere were redistributed (Phase 7).
    # Equal to target_quota until that happens.
    final_quota: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    raw_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qualified_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    selected_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[PortalRunStatus] = mapped_column(
        enum_column(PortalRunStatus, "portal_run_status"),
        default=PortalRunStatus.PENDING,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text)

    # How far the freshness escalation had to go (CLAUDE.md section 6). Null
    # until the leg runs.
    deepest_window: Mapped[FreshnessWindow | None] = mapped_column(
        enum_column(FreshnessWindow, "freshness_window")
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    search_run: Mapped[SearchRun] = relationship(
        back_populates="portals", lazy="raise"
    )
    portal: Mapped[Portal] = relationship(lazy="raise")
    queries: Mapped[list[SearchQuery]] = relationship(
        back_populates="search_run_portal",
        cascade="all, delete-orphan",
        lazy="raise",
    )
    raw_jobs: Mapped[list[RawJobRecord]] = relationship(
        back_populates="search_run_portal",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    @property
    def shortfall(self) -> int:
        """Slots this portal could not fill from qualified jobs."""
        return max(self.final_quota - self.selected_count, 0)

    def __repr__(self) -> str:
        return (
            f"<SearchRunPortal run={self.search_run_id} portal={self.portal_id} "
            f"quota={self.target_quota} status={self.status}>"
        )


class SearchQuery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One query issued against one portal in one freshness window.

    Query text is always a same-role title variation (CLAUDE.md section 3);
    unrelated roles are never searched, and this table is the audit trail that
    proves it.
    """

    __tablename__ = "search_queries"
    __table_args__ = (
        Index(
            "ix_search_queries_portal_leg_window",
            "search_run_portal_id",
            "window",
        ),
    )

    search_run_portal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("search_run_portals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    query_text: Mapped[str] = mapped_column(String(256), nullable=False)
    window: Mapped[FreshnessWindow] = mapped_column(
        enum_column(FreshnessWindow, "freshness_window"), nullable=False
    )
    page: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    search_run_portal: Mapped[SearchRunPortal] = relationship(
        back_populates="queries", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<SearchQuery {self.query_text!r} window={self.window}>"


class PortalError(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A portal failure, recorded so the run can carry on without it.

    Separate from ``portals`` on purpose: an outage is an event, not a change
    to the portal's configuration.
    """

    __tablename__ = "portal_errors"
    __table_args__ = (
        Index("ix_portal_errors_portal_id_created_at", "portal_id", "created_at"),
    )

    portal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Both nullable: a health check can fail outside any run.
    search_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE"), index=True
    )
    search_run_portal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("search_run_portals.id", ondelete="CASCADE")
    )

    kind: Mapped[PortalErrorKind] = mapped_column(
        enum_column(PortalErrorKind, "portal_error_kind"),
        default=PortalErrorKind.UNKNOWN,
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    # Which request failed. Never a resume or any candidate data.
    url: Mapped[str | None] = mapped_column(String(2048))
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    portal: Mapped[Portal] = relationship(lazy="raise")

    def __repr__(self) -> str:
        return f"<PortalError portal={self.portal_id} kind={self.kind}>"
