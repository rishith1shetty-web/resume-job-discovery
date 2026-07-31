"""User account.

Phase 1 has no authentication; this table exists because every resume,
search run and application is scoped to a user (CLAUDE.md section 11).
The auth scheme is deferred - see docs/FRONTEND_INTEGRATION.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.resume import Resume


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    resumes: Mapped[list[Resume]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",  # force explicit eager loading; no surprise IO in async
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"
