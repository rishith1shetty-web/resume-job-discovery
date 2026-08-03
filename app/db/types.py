"""Shared column type helpers."""

from __future__ import annotations

from enum import Enum as PyEnum

from sqlalchemy import Enum


def enum_column(enum_cls: type[PyEnum], name: str, length: int = 40) -> Enum:
    """A ``StrEnum`` column stored as VARCHAR + CHECK.

    Deliberately not a native PostgreSQL enum: adding a value to a native enum
    needs ``ALTER TYPE`` and cannot be done inside every migration context,
    whereas a CHECK constraint is rewritten freely. ``length`` leaves headroom
    so a longer member name stays a data-only change.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda enum: [member.value for member in enum],
    )
