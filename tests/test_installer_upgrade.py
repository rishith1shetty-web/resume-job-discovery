"""Installing over an existing installation.

# The failure this exists to prevent

A client with the previous version installed ran the new installer. It found
their role, found their database, kept both - and then died applying migration
0004:

    asyncpg.exceptions.InsufficientPrivilegeError:
    permission denied for schema public
    ... CREATE TABLE career_page_interactions (...)

The installer builds an administrator URL and hands it to Alembic. It always
had. But ``migrations/env.py`` overwrote it with the URL from application
settings, and on an upgrade those settings resolve - through
``_apply_installed_credential`` - to the stored least-privilege runtime
credential. So the DDL ran as ``resume_job_discovery_app``, which has data
rights and nothing else.

A fresh install was unaffected for a reason that made this hard to see: the
credential is written *after* migrations, so on a first install there is
nothing for settings to override with and the administrator URL survives. And
an upgrade before 0004 was unaffected too - the runtime role can SELECT
``alembic_version``, so an upgrade with no new revision read the version, found
head, and did nothing. The wrong role succeeded silently until the first real
schema change met it.

# What these tests do

They are not source inspection. They build a genuine previous-version
installation - a real database migrated to 0003, holding real rows, with a real
runtime role restricted exactly the way the installer restricts it - arm the
precise trap that caught the client (installed mode, stored credential), and
then run the installer's own ``run_migrations``.

The scenario is expensive, so it is performed once by a module-scoped fixture
that records what it observed; each test then names one property of it.

# Safety

These tests own two scratch databases and one scratch role, all three named so
they cannot collide with anything real. ``_refuse_protected_names`` is checked
before any DDL: the development database, the suite's own database and the
separate auto-apply project's database are all rejected by name, and the test
fails rather than touching them.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.core import credentials, paths
from app.core.config import get_settings

# --- Scratch names ----------------------------------------------------------

UPGRADE_DATABASE = "resume_job_discovery_upgrade_test"
FRESH_DATABASE = "resume_job_discovery_fresh_install_test"
SCRATCH_ROLE = "resume_job_discovery_upgrade_test_app"

#: Never created, dropped, migrated or written by this module. The development
#: database holds real jobs; the suite's own database is shared with every
#: other test; the third belongs to a different project entirely.
PROTECTED_DATABASES = frozenset(
    {
        "resume_job_discovery",
        "resume_job_discovery_test",
        "linkedin_auto_apply",
        "postgres",
    }
)

#: The revision the client already had installed.
PREVIOUS_REVISION = "0003_job_search_application"

#: The revision that failed on their machine.
CAREER_PAGE_REVISION = "0004_career_page_interactions"


def head_revision() -> str:
    """The current head, read from the migration scripts themselves.

    Not a pinned constant. The bug these tests exist for is "an existing
    database does not reach head", and pinning the revision number would turn
    every later migration into a failure of this file rather than a check that
    the upgrade still works. Read from `ScriptDirectory` so adding a migration
    extends the assertion instead of breaking it.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(paths.PROJECT_ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", str(paths.PROJECT_ROOT / "migrations")
    )
    return ScriptDirectory.from_config(config).get_current_head()


def _refuse_protected_names(*databases: str) -> None:
    for database in databases:
        if database in PROTECTED_DATABASES:
            pytest.fail(
                f"This test would have operated on {database!r}, which it must "
                "never touch. Refusing."
            )


def _provision_module() -> Any:
    sys.path.insert(0, str(paths.PROJECT_ROOT / "installer"))
    try:
        import provision

        return provision
    finally:
        sys.path.pop(0)


@contextmanager
def _pointed_at(provision: Any, *, database: str, role: str) -> Iterator[None]:
    """Aim the installer's module-level constants at the scratch objects.

    The grant and lifecycle functions read ``APP_DATABASE`` and ``APP_ROLE``
    directly. Redirecting them is what lets these tests exercise the
    installer's real SQL instead of a copy of it that could drift out of step.
    """
    previous = (provision.APP_DATABASE, provision.APP_ROLE)
    provision.APP_DATABASE, provision.APP_ROLE = database, role
    try:
        yield
    finally:
        provision.APP_DATABASE, provision.APP_ROLE = previous


# --- Connection helpers -----------------------------------------------------


def _admin_parameters() -> dict[str, Any]:
    """Superuser connection details, from the developer's own .env."""
    settings = get_settings()
    return {
        "user": settings.postgres_user,
        "password": settings.postgres_password,
        "host": settings.postgres_host,
        "port": settings.postgres_port,
    }


async def _connect(database: str, **overrides: Any) -> asyncpg.Connection:
    parameters = _admin_parameters() | overrides
    return await asyncpg.connect(database=database, timeout=15, **parameters)


async def _maintenance() -> asyncpg.Connection:
    """A connection to the maintenance database, for CREATE/DROP DATABASE."""
    return await _connect("postgres")


async def _recreate_scratch_database(database: str) -> None:
    """Give this run a clean scratch database, and only ever a scratch one."""
    _refuse_protected_names(database)

    connection = await _maintenance()
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await connection.execute(f'CREATE DATABASE "{database}"')
    finally:
        await connection.close()


async def _drop_scratch_database(database: str) -> None:
    _refuse_protected_names(database)
    connection = await _maintenance()
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await connection.close()


async def _database_oid(database: str) -> int | None:
    connection = await _maintenance()
    try:
        return await connection.fetchval(
            "SELECT oid FROM pg_database WHERE datname = $1", database
        )
    finally:
        await connection.close()


async def _tables_in(database: str) -> set[str]:
    connection = await _connect(database)
    try:
        rows = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        return {row["tablename"] for row in rows}
    finally:
        await connection.close()


def _run(coroutine: Any) -> Any:
    """Run one coroutine on its own loop, from a synchronous fixture."""
    return asyncio.run(coroutine)


async def _postgres_is_reachable() -> None:
    """Open and close one administrator connection, or raise."""
    connection = await _maintenance()
    await connection.close()


# --- Building a previous-version installation -------------------------------


def _alembic_upgrade(url: str, revision: str) -> None:
    """Apply migrations through the same channel the installer uses."""
    provision = _provision_module()

    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(paths.PROJECT_ROOT / "migrations"))
    config.attributes[provision.MIGRATION_URL_ATTRIBUTE] = url
    command.upgrade(config, revision)


async def _install_previous_version(provision: Any, database: str) -> uuid.UUID:
    """Migrate to 0003 and leave a row of real application data behind."""
    admin = _admin_parameters()
    url = provision.admin_url(admin["password"], admin["host"], admin["port"])

    # env.py drives an async engine with asyncio.run(), which will not start
    # while this thread already has a loop running.
    await asyncio.to_thread(_alembic_upgrade, url, PREVIOUS_REVISION)

    user_id = uuid.uuid4()
    connection = await _connect(database)
    try:
        await connection.execute(
            "INSERT INTO users (id, email, full_name, is_active) "
            "VALUES ($1, $2, $3, true)",
            user_id,
            "pytest+upgrade@test.invalid",
            "Existing Client Data",
        )
        await connection.execute(
            "INSERT INTO resumes (id, user_id, original_filename, storage_path, "
            "content_type, file_size_bytes, checksum_sha256, status, is_active) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, 'parsed', true)",
            uuid.uuid4(),
            user_id,
            "existing-resume.pdf",
            "storage/existing-resume.pdf",
            "application/pdf",
            1024,
            "0" * 64,
        )
    finally:
        await connection.close()
    return user_id


async def _create_scratch_role(role: str, password: str) -> None:
    connection = await _maintenance()
    try:
        # Left over from an interrupted run: remove it before recreating, so a
        # stale password cannot make this run mysterious.
        await connection.execute(f'DROP ROLE IF EXISTS "{role}"')
        await connection.execute(
            f'CREATE ROLE "{role}" WITH LOGIN PASSWORD \'{password}\' '
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION"
        )
    finally:
        await connection.close()


async def _drop_scratch_role(role: str, databases: tuple[str, ...]) -> None:
    """Remove the role and the grants that depend on it, in that order.

    PostgreSQL refuses to drop a role anything still depends on, and those
    dependencies live inside each database rather than globally.
    """
    for database in databases:
        try:
            connection = await _connect(database)
        except asyncpg.PostgresError:
            continue
        try:
            await connection.execute(f'DROP OWNED BY "{role}" CASCADE')
        except asyncpg.PostgresError:
            pass
        finally:
            await connection.close()

    connection = await _maintenance()
    try:
        await connection.execute(f'DROP ROLE IF EXISTS "{role}"')
    except asyncpg.PostgresError:
        pass
    finally:
        await connection.close()


async def _ddl_refusal(database: str, role: str, password: str) -> str | None:
    """Try to create a table as the runtime role. Returns the refusal, if any."""
    connection = await _connect(database, user=role, password=password)
    try:
        await connection.execute("CREATE TABLE privilege_probe (id integer)")
    except asyncpg.InsufficientPrivilegeError as error:
        return str(error)
    else:
        # It succeeded, so the role is not restricted and the scenario proves
        # nothing. Clean up, and let the assertion be the only symptom.
        await connection.execute("DROP TABLE privilege_probe")
        return None
    finally:
        await connection.close()


@contextmanager
def _installed_with_stored_credential(
    data_dir: Path, credential: credentials.DatabaseCredential
) -> Iterator[None]:
    """Arm the exact trap that caught the client.

    Installed mode plus a stored credential is what makes application settings
    resolve to the runtime role. Without both, the bug cannot reproduce and a
    passing test would mean nothing.
    """
    previous = os.environ.get(paths.DATA_DIR_ENV)
    os.environ[paths.DATA_DIR_ENV] = str(data_dir)
    paths.data_dir.cache_clear()
    get_settings.cache_clear()
    paths.ensure_data_tree()
    credentials.save_credential(credential)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(paths.DATA_DIR_ENV, None)
        else:
            os.environ[paths.DATA_DIR_ENV] = previous
        paths.data_dir.cache_clear()
        get_settings.cache_clear()


# --- The recorded scenario --------------------------------------------------


@dataclass
class UpgradeObservations:
    """Everything the upgrade run revealed, gathered once."""

    database_existed: bool = False
    installer_messages: str = ""
    role_kept_password: bool = False
    settings_url_during_migration: str = ""
    runtime_ddl_refused_before: str | None = None
    runtime_ddl_refused_after: str | None = None
    migration_error: BaseException | None = None
    tables_before: set[str] = field(default_factory=set)
    tables_after: set[str] = field(default_factory=set)
    revision_after: str | None = None
    oid_before: int | None = None
    oid_after: int | None = None
    user_id: uuid.UUID | None = None
    surviving_user: str | None = None
    surviving_resumes: int = 0
    runtime_privileges: set[str] = field(default_factory=set)
    career_page_rows_readable: bool = False


async def _perform_upgrade() -> UpgradeObservations:
    provision = _provision_module()
    observed = UpgradeObservations()
    admin = _admin_parameters()
    role_password = "upgrade-test-" + uuid.uuid4().hex

    await _recreate_scratch_database(UPGRADE_DATABASE)
    await _create_scratch_role(SCRATCH_ROLE, role_password)

    with _pointed_at(provision, database=UPGRADE_DATABASE, role=SCRATCH_ROLE):
        # 1. The previous version, as the client actually has it: schema at
        #    0003, real rows, and a runtime role holding only data rights.
        observed.user_id = await _install_previous_version(provision, UPGRADE_DATABASE)
        setup = provision.Progress()
        await provision.grant_connect(
            admin["host"], admin["port"], admin["password"], setup
        )
        await provision.grant_runtime_privileges(
            admin["host"], admin["port"], admin["password"], setup
        )

        observed.tables_before = await _tables_in(UPGRADE_DATABASE)
        observed.oid_before = await _database_oid(UPGRADE_DATABASE)
        observed.runtime_ddl_refused_before = await _ddl_refusal(
            UPGRADE_DATABASE, SCRATCH_ROLE, role_password
        )

        # 2. The new installer runs. It finds both and keeps them.
        detected = provision.Progress()
        maintenance = await _maintenance()
        try:
            returned_password = await provision.ensure_role(
                maintenance, detected, reuse_password=role_password
            )
            observed.role_kept_password = returned_password == role_password
            observed.database_existed = await provision.ensure_database(
                maintenance, detected
            )
        finally:
            await maintenance.close()
        observed.installer_messages = " ".join(detected.steps)

        # 3. Migrations, with the trap armed.
        credential = credentials.DatabaseCredential(
            SCRATCH_ROLE,
            role_password,
            admin["host"],
            admin["port"],
            UPGRADE_DATABASE,
        )
        url = provision.admin_url(admin["password"], admin["host"], admin["port"])

        with tempfile.TemporaryDirectory(prefix="jobfinder-upgrade-") as data_dir:
            with _installed_with_stored_credential(Path(data_dir), credential):
                # Recorded, not assumed: this is the value that used to be
                # taken as the migration target.
                observed.settings_url_during_migration = get_settings().sqlalchemy_url
                try:
                    await asyncio.to_thread(
                        provision.run_migrations, url, provision.Progress()
                    )
                except BaseException as error:  # noqa: BLE001 - recorded, then asserted
                    observed.migration_error = error

        # 4. Grants are re-applied over the new table, as the installer does.
        if observed.migration_error is None:
            await provision.grant_runtime_privileges(
                admin["host"], admin["port"], admin["password"], provision.Progress()
            )

    observed.tables_after = await _tables_in(UPGRADE_DATABASE)
    observed.oid_after = await _database_oid(UPGRADE_DATABASE)

    connection = await _connect(UPGRADE_DATABASE)
    try:
        observed.revision_after = await connection.fetchval(
            "SELECT version_num FROM alembic_version"
        )
        observed.surviving_user = await connection.fetchval(
            "SELECT full_name FROM users WHERE id = $1", observed.user_id
        )
        observed.surviving_resumes = await connection.fetchval(
            "SELECT count(*) FROM resumes WHERE user_id = $1", observed.user_id
        )
    finally:
        await connection.close()

    observed.runtime_ddl_refused_after = await _ddl_refusal(
        UPGRADE_DATABASE, SCRATCH_ROLE, role_password
    )

    runtime = await _connect(UPGRADE_DATABASE, user=SCRATCH_ROLE, password=role_password)
    try:
        rows = await runtime.fetch(
            "SELECT privilege_type FROM information_schema.table_privileges "
            "WHERE grantee = $1 AND table_schema = 'public' "
            "AND table_name = 'career_page_interactions'",
            SCRATCH_ROLE,
        )
        observed.runtime_privileges = {row["privilege_type"] for row in rows}
        try:
            await runtime.fetchval("SELECT count(*) FROM career_page_interactions")
            observed.career_page_rows_readable = True
        except asyncpg.PostgresError:
            observed.career_page_rows_readable = False
    finally:
        await runtime.close()

    return observed


@pytest.fixture(scope="module")
def upgrade() -> Iterator[UpgradeObservations]:
    """Perform the upgrade once; every test below names one property of it."""
    if not credentials.is_supported():
        pytest.skip("DPAPI is unavailable, so the stored credential cannot be armed.")

    try:
        _run(_postgres_is_reachable())
    except Exception as error:  # noqa: BLE001 - environment, not a defect
        pytest.skip(f"PostgreSQL is unavailable for provisioning tests: {error}")

    try:
        yield _run(_perform_upgrade())
    finally:
        _run(_drop_scratch_role(SCRATCH_ROLE, (UPGRADE_DATABASE,)))
        _run(_drop_scratch_database(UPGRADE_DATABASE))


# --- What the upgrade must do -----------------------------------------------


def test_the_scenario_is_actually_the_broken_one(upgrade: UpgradeObservations) -> None:
    """Guard on the guard: prove the trap was armed.

    If application settings had resolved to the administrator during the
    migration, every test below would pass without testing anything - the bug
    only exists when settings point somewhere else.
    """
    assert SCRATCH_ROLE in upgrade.settings_url_during_migration, (
        "Application settings did not resolve to the runtime role during the "
        "migration, so this run did not reproduce the client's situation."
    )
    assert upgrade.runtime_ddl_refused_before is not None, (
        "The runtime role could create tables, so it was not restricted the "
        "way the installer restricts it and nothing here is being tested."
    )
    assert "permission denied for schema public" in upgrade.runtime_ddl_refused_before


def test_the_existing_database_is_detected_and_kept(
    upgrade: UpgradeObservations,
) -> None:
    assert upgrade.database_existed is True
    assert "already exists; keeping it and its data" in upgrade.installer_messages


def test_the_existing_role_is_kept_with_its_credential(
    upgrade: UpgradeObservations,
) -> None:
    assert upgrade.role_kept_password is True
    assert "already exists; keeping its credential" in upgrade.installer_messages


def test_the_administrator_applies_the_migration(upgrade: UpgradeObservations) -> None:
    """The regression itself: this is where the client's install died."""
    assert upgrade.migration_error is None, (
        "Migrations did not run as the administrator. The client's installer "
        f"failed here: {upgrade.migration_error!r}"
    )


def test_migration_0004_reaches_head(upgrade: UpgradeObservations) -> None:
    assert upgrade.revision_after == head_revision()


def test_career_page_interactions_is_created(upgrade: UpgradeObservations) -> None:
    assert "career_page_interactions" not in upgrade.tables_before
    assert "career_page_interactions" in upgrade.tables_after


def test_existing_application_data_survives(upgrade: UpgradeObservations) -> None:
    assert upgrade.surviving_user == "Existing Client Data"
    assert upgrade.surviving_resumes == 1
    # Nothing the previous version created went missing on the way.
    assert upgrade.tables_before <= upgrade.tables_after


def test_the_database_is_never_recreated(upgrade: UpgradeObservations) -> None:
    """A dropped and rebuilt database would take the client's history with it.

    The OID is the check that cannot be faked: PostgreSQL assigns a new one to
    every CREATE DATABASE, so an unchanged OID means this is the same database
    throughout and not a replacement that happens to hold similar rows.
    """
    assert upgrade.oid_before is not None
    assert upgrade.oid_after == upgrade.oid_before


def test_the_runtime_role_keeps_data_access_to_the_new_table(
    upgrade: UpgradeObservations,
) -> None:
    assert {"SELECT", "INSERT", "UPDATE", "DELETE"} <= upgrade.runtime_privileges
    assert upgrade.career_page_rows_readable is True


def test_the_runtime_role_still_cannot_change_the_schema(
    upgrade: UpgradeObservations,
) -> None:
    """The fix must not have been "give the application role CREATE"."""
    assert upgrade.runtime_ddl_refused_after is not None, (
        "The application role can now create tables in schema public. The "
        "upgrade was fixed by broadening runtime privileges, which is exactly "
        "what must not happen."
    )
    assert "permission denied for schema public" in upgrade.runtime_ddl_refused_after


# --- The fresh-install path must not regress --------------------------------


@dataclass
class FreshObservations:
    migration_error: BaseException | None = None
    tables: set[str] = field(default_factory=set)
    revision: str | None = None


async def _perform_fresh_install() -> FreshObservations:
    provision = _provision_module()
    observed = FreshObservations()
    admin = _admin_parameters()

    await _recreate_scratch_database(FRESH_DATABASE)

    with _pointed_at(provision, database=FRESH_DATABASE, role=SCRATCH_ROLE):
        url = provision.admin_url(admin["password"], admin["host"], admin["port"])

        # Installed mode, and no credential: a first install writes the
        # credential only after migrations, so there is nothing to override
        # settings with. This is why the fresh path always worked, and it has
        # to keep working.
        with tempfile.TemporaryDirectory(prefix="jobfinder-fresh-") as data_dir:
            previous = os.environ.get(paths.DATA_DIR_ENV)
            os.environ[paths.DATA_DIR_ENV] = data_dir
            paths.data_dir.cache_clear()
            get_settings.cache_clear()
            try:
                paths.ensure_data_tree()
                await asyncio.to_thread(
                    provision.run_migrations, url, provision.Progress()
                )
            except BaseException as error:  # noqa: BLE001 - recorded, then asserted
                observed.migration_error = error
            finally:
                if previous is None:
                    os.environ.pop(paths.DATA_DIR_ENV, None)
                else:
                    os.environ[paths.DATA_DIR_ENV] = previous
                paths.data_dir.cache_clear()
                get_settings.cache_clear()

    observed.tables = await _tables_in(FRESH_DATABASE)
    connection = await _connect(FRESH_DATABASE)
    try:
        observed.revision = await connection.fetchval(
            "SELECT version_num FROM alembic_version"
        )
    finally:
        await connection.close()
    return observed


@pytest.fixture(scope="module")
def fresh() -> Iterator[FreshObservations]:
    try:
        _run(_postgres_is_reachable())
    except Exception as error:  # noqa: BLE001 - environment, not a defect
        pytest.skip(f"PostgreSQL is unavailable for provisioning tests: {error}")

    try:
        yield _run(_perform_fresh_install())
    finally:
        _run(_drop_scratch_database(FRESH_DATABASE))


def test_a_fresh_install_still_migrates_to_head(fresh: FreshObservations) -> None:
    assert fresh.migration_error is None, (
        f"A first installation no longer provisions: {fresh.migration_error!r}"
    )
    assert fresh.revision == head_revision()


def test_a_fresh_install_creates_every_table_the_installer_verifies(
    fresh: FreshObservations,
) -> None:
    provision = _provision_module()

    missing = [table for table in provision.REQUIRED_TABLES if table not in fresh.tables]
    assert not missing, f"A fresh install is missing: {missing}"


# --- The contract that carries the URL --------------------------------------


def test_the_installer_and_alembic_agree_on_the_attribute_name() -> None:
    """Two files, one key. A rename in either silently restores the bug."""
    provision = _provision_module()
    source = (paths.PROJECT_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")

    assert f'URL_ATTRIBUTE = "{provision.MIGRATION_URL_ATTRIBUTE}"' in source, (
        "migrations/env.py no longer reads the key installer/provision.py "
        "writes, so the administrator URL would be discarded again."
    )


def test_env_py_prefers_the_supplied_url_over_application_settings() -> None:
    """The line that caused the outage was an unconditional override."""
    source = (paths.PROJECT_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")

    # The override must be consulted before settings are even imported, so a
    # caller that names its own connection never depends on application config.
    supplied_at = source.index("config.attributes.get(URL_ATTRIBUTE)")
    settings_at = source.index("get_settings().sqlalchemy_url")
    assert supplied_at < settings_at

    assert 'config.set_main_option("sqlalchemy.url"' not in source, (
        "The URL must not be written back into the ini section: ConfigParser "
        "interpolates '%', and a percent-encoded password would raise before a "
        "connection was attempted."
    )
