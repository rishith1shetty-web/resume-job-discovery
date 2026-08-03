"""Uploaded resume file.

Phase 1 stores file metadata only. Parsing into a structured profile and the
``resume_profiles`` table arrive in Phase 2; this table gains no auto-apply
fields at any point.

CLAUDE.md section 18: the original resume file is never altered or rewritten.
``storage_path`` points at an immutable copy.
"""

from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy resolves
# `Mapped[uuid.UUID]` annotations by evaluating them in the module namespace.
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ResumeStatus
from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.profile import ResumeProfile, ResumeText
    from app.models.search import SearchRun
    from app.models.user import User


class Resume(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "resumes"
    __table_args__ = (
        # Re-uploading a byte-identical file should resolve to the existing
        # resume rather than fragmenting that resume's search history.
        UniqueConstraint("user_id", "checksum_sha256"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # Location of the stored copy. Local path now; an object-storage key later.
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[ResumeStatus] = mapped_column(
        Enum(
            ResumeStatus,
            name="resume_status",
            native_enum=False,  # VARCHAR + CHECK; far easier to evolve
            create_constraint=True,  # SQLAlchemy 2.0 defaults this to False
            length=32,  # headroom so a new status value is a data-only change
            values_callable=lambda enum: [member.value for member in enum],
        ),
        default=ResumeStatus.UPLOADED,
        nullable=False,
        index=True,
    )
    # The resume a new search run targets by default.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="resumes", lazy="raise")

    text: Mapped[ResumeText | None] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="raise",
    )
    # Every analysis run, newest first. Superseded versions are retained so an
    # earlier detection stays traceable.
    profiles: Mapped[list[ResumeProfile]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
        order_by="desc(ResumeProfile.created_at)",
        lazy="raise",
    )
    # Search history is per resume. Two resumes never share runs, results or
    # application state (CLAUDE.md section 11).
    search_runs: Mapped[list[SearchRun]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
        order_by="desc(SearchRun.created_at)",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Resume id={self.id} filename={self.original_filename!r}>"
