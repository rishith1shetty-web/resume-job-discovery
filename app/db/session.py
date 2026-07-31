"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.sqlalchemy_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # drop connections killed by an idle timeout
        future=True,
    )


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create the process-wide engine. Called from the app lifespan."""
    global _engine, _sessionmaker

    if _engine is None:
        settings = settings or get_settings()
        _engine = create_engine(settings)
        _sessionmaker = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,  # keep attributes usable after commit
            autoflush=False,
        )
        logger.info(
            "database_engine_initialised",
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_db,
        )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None  # narrowed by init_engine
    return _sessionmaker


async def dispose_engine() -> None:
    """Close all pooled connections. Called from the app lifespan on shutdown."""
    global _engine, _sessionmaker

    if _engine is not None:
        await _engine.dispose()
        logger.info("database_engine_disposed")
    _engine = None
    _sessionmaker = None


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session, rolled back on error.

    Routes are responsible for committing; anything that raises is rolled back
    here so a failed request can never leave a partial write behind.
    """
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def ping_database() -> bool:
    """Return True when the database answers a trivial query.

    Used by the health endpoint, which must report a degraded state rather
    than raise when the database is down.
    """
    try:
        engine = get_engine()
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - health check must never raise
        logger.warning("database_ping_failed", error_type=type(exc).__name__)
        return False
