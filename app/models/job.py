"""Canonical jobs, their portal sources, raw captures and resume-wise matches.

The shape that matters (CLAUDE.md section 9, docs/DECISIONS.md D-003):

``raw_jobs``    exactly what a portal returned, untouched. An audit trail.
``jobs``        one row per real vacancy, however many portals carry it.
``job_sources`` one row per portal that carries it, keeping that portal's own
                listing URL and application URL. Merging must never discard
                which portals surfaced a job.
``job_matches`` one score per (resume, job, run). Scores belong to a resume,
                never to a job, so two resumes never share a verdict.

Phase 3 defines this structure only. Deduplication, canonical-URL resolution
and scoring are Phases 5 and 6; the columns they will write are declared here
so their migrations are additive rather than a redesign.
"""

from __future__ import annotations

# Imported at runtime: SQLAlchemy evaluates `Mapped[uuid.UUID]` annotations in
# the module namespace.
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import (
    SCORE_MAX,
    SCORE_MIN,
    JobUrlKind,
    RawJobStatus,
    WorkMode,
)
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column

if TYPE_CHECKING:
    from app.models.portal import Portal
    from app.models.resume import Resume
    from app.models.search import SearchRun, SearchRunPortal

_SCORE_RANGE = f"BETWEEN {SCORE_MIN} AND {SCORE_MAX}"


def _utcnow() -> datetime:
    """Wall-clock now, which advances inside a transaction unlike SQL now()."""
    return datetime.now(UTC)

#: Every component score on a job match, in the order the dashboard shows them.
SCORE_COLUMNS: tuple[str, ...] = (
    "final_score",
    "role_score",
    "skill_score",
    "experience_score",
    "responsibility_score",
    "domain_score",
    "education_score",
    "freshness_score",
    "ranking_score",
)


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One canonical vacancy.

    Not scoped to a user or resume: the same vacancy is one row no matter how
    many resumes it is scored against. Everything resume-specific lives in
    ``job_matches`` and ``applications``.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # Dedup identity computed in Phase 5 from ATS id, canonical URL,
        # normalised company+title+location and description fingerprint -
        # never from the URL alone (CLAUDE.md section 9).
        UniqueConstraint("canonical_key"),
        Index(
            "ix_jobs_normalized_company_title",
            "normalized_company",
            "normalized_title",
        ),
        Index("ix_jobs_posted_at", "posted_at"),
        # Partial: the common query is "still-active jobs", and most rows have
        # expired_at NULL.
        Index(
            "ix_jobs_active_last_seen",
            "last_seen_at",
            postgresql_where=text("expired_at IS NULL"),
        ),
    )

    canonical_key: Mapped[str] = mapped_column(String(128), nullable=False)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_title: Mapped[str | None] = mapped_column(String(512), index=True)
    company: Mapped[str | None] = mapped_column(String(256))
    normalized_company: Mapped[str | None] = mapped_column(String(256))
    location: Mapped[str | None] = mapped_column(String(256))
    work_mode: Mapped[WorkMode | None] = mapped_column(
        enum_column(WorkMode, "work_mode")
    )
    employment_type: Mapped[str | None] = mapped_column(String(64))

    # Null when no portal exposed one. Section 7 then treats the job as
    # unscoreable rather than guessing - a Phase 6 decision, not a Phase 3 one.
    description: Mapped[str | None] = mapped_column(Text)
    description_fingerprint: Mapped[str | None] = mapped_column(
        String(64), index=True
    )

    # --- Freshness (CLAUDE.md section 6) ----------------------------------
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # "New" means first discovered in this search, not merely rediscovered.
    first_discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A job condition, never an application status: expiry must not overwrite a
    # user's APPLIED state (CLAUDE.md sections 10 and 11).
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    # --- Preferred URL (CLAUDE.md section 9) ------------------------------
    # Resolved in Phase 5 from the sources below, most direct first.
    preferred_application_url: Mapped[str | None] = mapped_column(String(2048))
    preferred_url_kind: Mapped[JobUrlKind | None] = mapped_column(
        enum_column(JobUrlKind, "job_url_kind")
    )

    sources: Mapped[list[JobSource]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobSource.created_at",
        lazy="raise",
    )
    matches: Mapped[list[JobMatch]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    @property
    def is_expired(self) -> bool:
        return self.expired_at is not None

    def __repr__(self) -> str:
        return f"<Job id={self.id} title={self.title!r} company={self.company!r}>"


class JobSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One portal's listing of a canonical job.

    Both URLs are kept because they answer different questions: ``source_url``
    is where the listing was found, ``application_url`` is where the user
    actually applies. An external Apply link that redirects to an employer page
    or an ATS form is fine and expected - the user opens it and applies
    manually (docs/DECISIONS.md D-002).
    """

    __tablename__ = "job_sources"
    __table_args__ = (
        # One source row per portal per canonical job, so a rediscovery updates
        # the existing row instead of accumulating near-duplicates.
        UniqueConstraint("job_id", "portal_id"),
        # A portal's own id for a listing resolves to exactly one canonical
        # job. Partial, because most portals do not expose a stable id.
        Index(
            "uq_job_sources_portal_external_id",
            "portal_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    portal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portals.id", ondelete="CASCADE"), nullable=False, index=True
    )

    external_id: Mapped[str | None] = mapped_column(String(256))
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    application_url: Mapped[str | None] = mapped_column(String(2048))
    url_kind: Mapped[JobUrlKind] = mapped_column(
        enum_column(JobUrlKind, "job_url_kind"),
        default=JobUrlKind.UNKNOWN,
        nullable=False,
    )

    # This portal's own view of the posting date; portals frequently disagree.
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # True for the source that supplied the job's preferred URL (Phase 5).
    is_preferred: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    job: Mapped[Job] = relationship(back_populates="sources", lazy="raise")
    portal: Mapped[Portal] = relationship(lazy="raise")

    def __repr__(self) -> str:
        return f"<JobSource job={self.job_id} portal={self.portal_id}>"


class RawJobRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A listing exactly as a portal returned it, before any processing.

    The persisted counterpart of ``app.portals.base.RawJob``: same fields, one
    transient and one stored. Kept verbatim so a later scoring version can
    re-extract from ``raw_payload`` without re-querying the portal.
    """

    __tablename__ = "raw_jobs"
    __table_args__ = (
        Index("ix_raw_jobs_portal_external_id", "portal_id", "external_id"),
        Index("ix_raw_jobs_leg_status", "search_run_portal_id", "status"),
    )

    search_run_portal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("search_run_portals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    portal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portals.id", ondelete="CASCADE"), nullable=False
    )
    search_query_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("search_queries.id", ondelete="CASCADE")
    )
    # Set once this capture is promoted to a canonical job (Phase 5).
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )

    external_id: Mapped[str | None] = mapped_column(String(256))
    # Portal slug as reported by the adapter, kept even if the portal row is
    # later renamed.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company: Mapped[str | None] = mapped_column(String(256))
    location: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    application_url: Mapped[str | None] = mapped_column(String(2048))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    employment_type: Mapped[str | None] = mapped_column(String(64))

    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    status: Mapped[RawJobStatus] = mapped_column(
        enum_column(RawJobStatus, "raw_job_status"),
        default=RawJobStatus.COLLECTED,
        nullable=False,
    )
    # Why this capture never became a job. Free-form, set in Phase 5/6.
    rejection_reason: Mapped[str | None] = mapped_column(String(128))
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    search_run_portal: Mapped[SearchRunPortal] = relationship(
        back_populates="raw_jobs", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<RawJobRecord source={self.source!r} title={self.title!r}>"


class JobMatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """How well one job fits one resume.

    Scored per resume, never per job: two resumes may reach opposite verdicts
    on the same vacancy, and neither may see the other's score (CLAUDE.md
    section 11). ``scoring_version`` records which logic produced the numbers
    so a change is traceable rather than silent.
    """

    __tablename__ = "job_matches"
    __table_args__ = (
        # One verdict per resume per job per run. Re-running a search records a
        # new row rather than overwriting the earlier judgement.
        UniqueConstraint("resume_id", "job_id", "search_run_id"),
        CheckConstraint(f"final_score {_SCORE_RANGE}", name="final_score_range"),
        CheckConstraint(f"role_score {_SCORE_RANGE}", name="role_score_range"),
        CheckConstraint(f"skill_score {_SCORE_RANGE}", name="skill_score_range"),
        Index(
            "ix_job_matches_resume_qualified_score",
            "resume_id",
            "qualified",
            "ranking_score",
        ),
        Index("ix_job_matches_search_run_id_qualified", "search_run_id", "qualified"),
    )

    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Null for a rescore performed outside any run.
    search_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE")
    )

    # --- Weighted components (CLAUDE.md section 7) ------------------------
    final_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    role_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    skill_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    experience_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    responsibility_score: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    domain_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    education_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Freshness helps prioritise; it never overrides relevance (section 8).
    freshness_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # What the dashboard sorts "Best Overall" by.
    ranking_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # False for anything below the locked threshold or hitting a hard reject.
    qualified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    rejection_reasons: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    # Per-component evidence, so a score can be explained to the user.
    score_details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    scoring_version: Mapped[str] = mapped_column(String(32), nullable=False)
    # Client-side default as well as the server one: PostgreSQL's now() is the
    # *transaction* timestamp, so two rescores inside one transaction would
    # share a timestamp and "the newest score" would be ambiguous. The server
    # default remains for rows inserted by direct SQL.
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        server_default=func.now(),
        nullable=False,
    )

    resume: Mapped[Resume] = relationship(lazy="raise")
    job: Mapped[Job] = relationship(back_populates="matches", lazy="raise")
    search_run: Mapped[SearchRun | None] = relationship(lazy="raise")

    def __repr__(self) -> str:
        return (
            f"<JobMatch resume={self.resume_id} job={self.job_id} "
            f"score={self.final_score} qualified={self.qualified}>"
        )
