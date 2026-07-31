"""Extracted resume text and analysed profile.

Three tables, matching the Phase 2 scope:

``resume_texts``    the text pulled out of the uploaded file, stored apart
                    from the resume record so the original file stays
                    untouched and re-analysis never needs to re-read it.
``resume_profiles`` one row per analysis run. Runs are versioned rather than
                    overwritten, so an earlier detection stays traceable after
                    the logic changes.
``resume_skills``   normalised skills, kept relational because Phase 6
                    matching needs set operations over them.
"""

from __future__ import annotations

# Imported at runtime: SQLAlchemy evaluates `Mapped[uuid.UUID]` annotations in
# the module namespace.
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import Seniority, SkillKind
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.resume import Resume


def _enum_column(enum_cls: type, name: str, length: int = 32) -> Enum:
    """VARCHAR + CHECK rather than a native PG enum, which is easier to evolve."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda enum: [member.value for member in enum],
    )


class ResumeText(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Text extracted from an uploaded resume.

    Separate from ``resumes`` so the file record stays small and the original
    file is never rewritten (CLAUDE.md section 18).
    """

    __tablename__ = "resume_texts"

    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer)

    # Which adapter produced this text, so a re-extraction under a different
    # library is identifiable.
    extractor: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(64), nullable=False)

    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    resume: Mapped[Resume] = relationship(back_populates="text", lazy="raise")

    def __repr__(self) -> str:
        return f"<ResumeText resume_id={self.resume_id} chars={self.char_count}>"


class ResumeProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One analysis run over one resume.

    Detected values are never overwritten. A user correction is stored in the
    ``confirmed_*`` columns alongside them, so the original detection and its
    evidence remain available for debugging a wrong result.
    """

    __tablename__ = "resume_profiles"
    __table_args__ = (
        # At most one active profile per resume, enforced by a partial unique
        # index so superseded versions can coexist.
        Index(
            "uq_resume_profiles_active_per_resume",
            "resume_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        Index("ix_resume_profiles_resume_id_created_at", "resume_id", "created_at"),
    )

    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # CLAUDE.md section 7: analyses stay traceable across logic changes.
    analysis_version: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, index=True
    )
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Detected domain (exactly one) ------------------------------------
    detected_domain: Mapped[str | None] = mapped_column(String(64))
    detected_domain_display: Mapped[str | None] = mapped_column(String(128))
    domain_confidence: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    domain_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    # --- Detected target role (exactly one) -------------------------------
    detected_target_role: Mapped[str | None] = mapped_column(String(64))
    detected_target_role_display: Mapped[str | None] = mapped_column(String(128))
    target_role_confidence: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    target_role_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    # --- User corrections --------------------------------------------------
    confirmed_domain: Mapped[str | None] = mapped_column(String(64))
    confirmed_target_role: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Seniority and experience -----------------------------------------
    seniority: Mapped[Seniority | None] = mapped_column(
        _enum_column(Seniority, "seniority_level")
    )
    seniority_confidence: Mapped[float] = mapped_column(
        Float, default=0.0, nullable=False
    )
    seniority_evidence: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    # Null when the resume gives nothing verifiable. Never guessed.
    years_experience: Mapped[float | None] = mapped_column(Float)
    years_experience_source: Mapped[str | None] = mapped_column(String(32))
    years_experience_evidence: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    # --- Structured extraction --------------------------------------------
    # Same-role search titles only (CLAUDE.md section 3).
    role_variations: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    titles: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    responsibilities: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    education: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    certifications: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    sections_found: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    resume: Mapped[Resume] = relationship(back_populates="profiles", lazy="raise")
    skills: Mapped[list[ResumeSkill]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    @property
    def effective_domain(self) -> str | None:
        """User correction when present, otherwise the detected value."""
        return self.confirmed_domain or self.detected_domain

    @property
    def effective_target_role(self) -> str | None:
        return self.confirmed_target_role or self.detected_target_role

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None

    def __repr__(self) -> str:
        return (
            f"<ResumeProfile resume_id={self.resume_id} "
            f"role={self.effective_target_role!r} v={self.analysis_version}>"
        )


class ResumeSkill(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A skill, technology or tool extracted for one profile.

    Relational rather than JSON because Phase 6 scoring intersects these
    against job requirements.
    """

    __tablename__ = "resume_skills"
    __table_args__ = (
        UniqueConstraint("profile_id", "skill_key"),
        Index("ix_resume_skills_skill_key", "skill_key"),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resume_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Taxonomy key, so comparisons never depend on surface spelling.
    skill_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[SkillKind] = mapped_column(
        _enum_column(SkillKind, "skill_kind"), nullable=False
    )

    occurrences: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    sections: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    evidence: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    profile: Mapped[ResumeProfile] = relationship(
        back_populates="skills", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<ResumeSkill {self.skill_key!r} kind={self.kind}>"
