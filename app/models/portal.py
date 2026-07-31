"""Portal registration and per-portal operational configuration.

CLAUDE.md sections 4 and 18: the portal list is *data*, never a hardcoded
list in the search engine. A portal row binds a stable ``slug`` to an
``adapter_key`` resolved at runtime through ``app.portals.registry``.

The central search service iterates whatever these rows resolve to. It must
never contain an ``if portal == ...`` chain.
"""

from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy resolves
# `Mapped[uuid.UUID]` annotations by evaluating them in the module namespace.
import uuid
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Portal(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "portals"

    # Stable identifier used in API payloads and by the adapter registry.
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    website_url: Mapped[str | None] = mapped_column(String(512))

    # Registry lookup key. Nullable because a portal may be registered as a
    # planned integration before its adapter is written.
    adapter_key: Mapped[str | None] = mapped_column(String(64))

    # CLAUDE.md section 4: at most 8 enabled at one time. Enforced in the
    # service layer, which can return a clear API error; a DB constraint here
    # would surface as an opaque integrity failure.
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    # False until an adapter exists and passes its contract tests (Phase 4).
    is_implemented: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Capability flags mirroring PortalCapabilities in app/portals/base.py.
    # Stored so the matching layer knows, before searching, whether a portal
    # can supply full descriptions - which drives the section 7 reject rule
    # for jobs that cannot be scored reliably.
    supports_date_filter: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    supports_description_fetch: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    requires_auth: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Operator note, e.g. access restrictions or terms-of-service limits.
    notes: Mapped[str | None] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    config: Mapped[PortalConfig] = relationship(
        back_populates="portal",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Portal slug={self.slug!r} enabled={self.is_enabled}>"


class PortalConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-portal timeout, retry and rate-limit policy (CLAUDE.md section 4).

    Kept in its own table so operational tuning does not require touching the
    portal's identity row, and so an adapter never hardcodes its own limits.
    """

    __tablename__ = "portal_configs"

    portal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("portals.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    timeout_seconds: Mapped[float] = mapped_column(Float, default=30.0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    retry_backoff_seconds: Mapped[float] = mapped_column(
        Float, default=2.0, nullable=False
    )
    # Respect portal terms and rate limits (CLAUDE.md section 15).
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer, default=30, nullable=False
    )
    max_concurrent_requests: Mapped[int] = mapped_column(
        Integer, default=2, nullable=False
    )
    # Upper bound on pagination per freshness window, so one portal cannot
    # consume the whole run's time budget.
    max_pages_per_window: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )

    # Adapter-specific settings (board tokens, endpoints, headers). Opaque to
    # the core system by design - core never inspects these keys.
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )

    portal: Mapped[Portal] = relationship(back_populates="config", lazy="raise")

    def __repr__(self) -> str:
        return f"<PortalConfig portal_id={self.portal_id}>"
