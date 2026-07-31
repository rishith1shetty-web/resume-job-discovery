"""Current-user resolution.

The application runs in single-user local mode for now, but the database keeps
its ``user_id``-based architecture unchanged. Every resume, profile and later
every search run and application is already scoped to a user row, so adding
authentication means replacing this one function - not redesigning the schema.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.user import User

logger = get_logger(__name__)


async def get_or_create_default_user(
    session: AsyncSession, settings: Settings
) -> User:
    """Return the single local user, creating it on first use.

    Created lazily rather than seeded by a migration, so the schema carries no
    environment-specific data.
    """
    email = settings.default_user_email.lower()

    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    user = User(
        email=email,
        full_name=settings.default_user_name,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    logger.info("default_user_created", user_id=str(user.id))
    return user
