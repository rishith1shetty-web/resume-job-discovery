"""Shared test fixtures.

Database safety rules enforced here:

* The configured database name must be exactly ``resume_job_discovery``.
  Anything else - above all ``linkedin_auto_apply`` - aborts the run.
* Integration tests run inside a transaction that is always rolled back, so no
  test data is ever committed.
* Records that are created carry an identifiable marker as a second line of
  defence, so anything that somehow escaped rollback is greppable.
* Nothing truncates, drops, resets or recreates the database, and no
  destructive migration command is ever run.
* File uploads are written to a pytest ``tmp_path``, never the real storage
  directory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings

REQUIRED_DATABASE = "resume_job_discovery"
FORBIDDEN_DATABASE = "linkedin_auto_apply"

# Marker on every record tests create. Greppable, and obviously not real data.
TEST_USER_EMAIL = "pytest+jobfinder@test.invalid"
TEST_RECORD_MARKER = "pytest-jobfinder"

# Only non-database settings are pinned. POSTGRES_* deliberately comes from the
# local .env so the guard below checks the configuration actually in use.
TEST_ENV = {
    "APP_NAME": "JobFinder",
    "ENVIRONMENT": "local",
    "DEBUG": "false",
    "CORS_ALLOW_ORIGINS": "http://localhost:5173,http://localhost:3000",
    "CORS_ALLOW_ORIGIN_REGEX": r"^https://.*\.lovable(project)?\.app$",
    "LOG_LEVEL": "WARNING",
    "LOG_FORMAT": "console",
    "DEFAULT_USER_EMAIL": TEST_USER_EMAIL,
    "ENABLE_LLM_ASSIST": "false",
}


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture(autouse=True)
def _guard_database_target(_test_env: None) -> None:
    """Refuse to run against anything but resume_job_discovery.

    Fails the test rather than skipping: a misconfigured target is a problem to
    surface, not to quietly work around.
    """
    current = get_settings()

    if current.postgres_db != REQUIRED_DATABASE:
        pytest.fail(
            f"Tests must target {REQUIRED_DATABASE!r}, but POSTGRES_DB is "
            f"{current.postgres_db!r}. Refusing to run."
        )
    if FORBIDDEN_DATABASE in current.sqlalchemy_url:
        pytest.fail(
            f"Configuration references {FORBIDDEN_DATABASE!r}. Refusing to run."
        )


@pytest.fixture(autouse=True)
def _database_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report the database as reachable for tests that do not use it.

    Keeps unit tests hermetic and fast. Integration tests use the real
    connection via ``db_session``.
    """

    async def _ping() -> bool:
        return True

    monkeypatch.setattr("app.db.session.ping_database", _ping)


# --------------------------------------------------------------------------
# Unit-test app (no database)
# --------------------------------------------------------------------------


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    from app.main import create_app

    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# --------------------------------------------------------------------------
# Integration fixtures (real database, always rolled back)
# --------------------------------------------------------------------------


@pytest.fixture
async def db_session(settings: Settings) -> AsyncIterator[AsyncSession]:
    """A session whose work is always rolled back.

    The session joins an already-open transaction using savepoints, so the
    application's own ``session.commit()`` calls succeed without committing
    anything permanently. The outer transaction is rolled back unconditionally.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.db.session import create_engine

    engine = create_engine(settings)

    try:
        connection = await engine.connect()
    except SQLAlchemyError as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL unavailable for integration tests: {type(exc).__name__}")

    transaction = await connection.begin()
    factory = async_sessionmaker(
        bind=connection,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    session = factory()

    try:
        yield session
    finally:
        await session.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    """Isolated storage root. Never the configured ./storage directory."""
    path = tmp_path / "storage"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
async def api_app(
    settings: Settings, db_session: AsyncSession, storage_dir: Path
) -> AsyncIterator[FastAPI]:
    """App wired to the rolled-back session and a temporary storage root."""
    from app.api.deps import _storage_dependency
    from app.db.session import get_db_session
    from app.main import create_app
    from app.services.storage import LocalResumeStorage

    application = create_app(settings)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    def _override_storage() -> LocalResumeStorage:
        return LocalResumeStorage(storage_dir)

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[_storage_dependency] = _override_storage

    yield application

    application.dependency_overrides.clear()


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
