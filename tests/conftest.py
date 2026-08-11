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

#: The suite has its own database, created on demand and migrated to head.
#: Fixtures must never meet real rows: `build_canonical_key` is deliberately
#: date-free, so a recorded listing deduplicates onto a real job with the same
#: company, title and location, and `earliest_posted_at` then keeps the older
#: stored date. A test's result would depend on what the developer happened to
#: search for last, which is not a test.
REQUIRED_DATABASE = "resume_job_discovery_test"

#: Never written to by the suite. Named so the guard can say why it refused.
DEVELOPMENT_DATABASE = "resume_job_discovery"
FORBIDDEN_DATABASE = "linkedin_auto_apply"

# Marker on every record tests create. Greppable, and obviously not real data.
TEST_USER_EMAIL = "pytest+jobfinder@test.invalid"
TEST_RECORD_MARKER = "pytest-jobfinder"

# POSTGRES_DB is pinned to the test database; the remaining POSTGRES_* values
# come from the local .env, so the suite reaches the same server with the same
# credentials and only the database differs.
TEST_ENV = {
    "POSTGRES_DB": REQUIRED_DATABASE,
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


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> None:
    """Create the suite's own database, migrated to head, once per session.

    Connects to the ``postgres`` maintenance database to issue CREATE DATABASE,
    because a session cannot create the database it is connected to. The
    development database is never opened, never read and never written.

    The database is left in place afterwards rather than dropped: creating and
    migrating it costs a few seconds, and keeping it makes a re-run fast. It is
    truncated per test by the transaction rollback in ``db_session``.
    """
    import asyncio
    import os

    import asyncpg

    base = get_settings()  # .env values: host, port, user, password

    async def _create_if_missing() -> None:
        # The maintenance database, because a session cannot create the
        # database it is connected to. The development database is never
        # opened.
        connection = await asyncpg.connect(
            user=base.postgres_user,
            password=base.postgres_password,
            host=base.postgres_host,
            port=base.postgres_port,
            database="postgres",
        )
        try:
            exists = await connection.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", REQUIRED_DATABASE
            )
            if not exists:
                # Identifier is a module constant, not user input.
                await connection.execute(f'CREATE DATABASE "{REQUIRED_DATABASE}"')
        finally:
            await connection.close()

    try:
        asyncio.run(_create_if_missing())
    except Exception as error:  # pragma: no cover - environment problem
        pytest.exit(
            f"Could not prepare the test database {REQUIRED_DATABASE!r}: {error}",
            returncode=1,
        )

    # env.py reads application settings, so the target is set before Alembic
    # imports it. Restored afterwards so nothing else sees the change.
    previous = os.environ.get("POSTGRES_DB")
    os.environ["POSTGRES_DB"] = REQUIRED_DATABASE
    get_settings.cache_clear()
    try:
        def _upgrade() -> None:
            from alembic import command
            from alembic.config import Config

            root = Path(__file__).resolve().parents[1]
            config = Config(str(root / "alembic.ini"))
            config.set_main_option("script_location", str(root / "migrations"))
            command.upgrade(config, "head")

        # migrations/env.py drives an async engine with asyncio.run(), which
        # raises if a loop is already running on this thread.
        asyncio.run(asyncio.to_thread(_upgrade))
    finally:
        if previous is None:
            os.environ.pop("POSTGRES_DB", None)
        else:
            os.environ["POSTGRES_DB"] = previous
        get_settings.cache_clear()


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
    """Refuse to run against anything but the dedicated test database.

    Fails the test rather than skipping: a misconfigured target is a problem to
    surface, not to quietly work around.
    """
    current = get_settings()

    if current.postgres_db == DEVELOPMENT_DATABASE:
        pytest.fail(
            f"Tests must never run against {DEVELOPMENT_DATABASE!r}: it holds "
            "real jobs, and fixtures would deduplicate onto them. Expected "
            f"{REQUIRED_DATABASE!r}."
        )
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
