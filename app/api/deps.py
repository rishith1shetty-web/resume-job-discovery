"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models.user import User
from app.services.storage import LocalResumeStorage, get_storage
from app.services.user_service import get_or_create_default_user

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _storage_dependency(settings: AppSettings) -> LocalResumeStorage:
    return get_storage(settings)


Storage = Annotated[LocalResumeStorage, Depends(_storage_dependency)]


async def _current_user(session: DbSession, settings: AppSettings) -> User:
    """Resolve the acting user.

    Single-user local mode: the default user is created on first use. When
    authentication is added, only this function changes - every table is
    already scoped by ``user_id``.
    """
    return await get_or_create_default_user(session, settings)


CurrentUser = Annotated[User, Depends(_current_user)]
