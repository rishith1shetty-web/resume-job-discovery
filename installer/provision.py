"""Provision this computer's database. Run once, by the installer.

The recruiter installing this application is not expected to know what a role
or a migration is. Everything between "I typed the postgres password" and "the
application works" happens here.

# What it does

    1. Connect to PostgreSQL as the administrator, using the password the
       installer collected.
    2. Create the least-privilege role `resume_job_discovery_app` with a
       randomly generated password of its own.
    3. Create the database `resume_job_discovery`, owned by that role.
    4. Apply least-privilege grants.
    5. Run every Alembic migration.
    6. Verify the resulting schema really contains the expected tables.
    7. Seed the portal rows and the local user.
    8. Write the generated configuration and store the credential with DPAPI.
    9. Prove the application can actually connect with what was just stored.

# Two properties this has to get right

**Idempotence.** Installing over an existing installation is the supported
upgrade path, so every step asks before it acts. An existing database is kept,
not recreated — the alternative destroys the recruiter's application history.

**Honest rollback.** If provisioning fails partway, only what *this run*
created is removed. A database that already held data is never dropped to
clean up a failed upgrade; that would trade a failed install for lost work.

# The administrator password

Read from stdin, never from argv. Command lines are visible to any process on
the machine through the process table, and Inno Setup's own log records the
parameters it execs with. It is held only as long as the connection needs it
and is never written anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The frozen build has no source tree on sys.path.
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402

from app.core import paths  # noqa: E402
from app.core.credentials import (  # noqa: E402
    DatabaseCredential,
    generate_password,
    load_credential,
    save_credential,
)

APP_ROLE = "resume_job_discovery_app"
APP_DATABASE = "resume_job_discovery"
ADMIN_USER = "postgres"
ADMIN_DATABASE = "postgres"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5432

#: Minimum server version. The product is specified against PostgreSQL 16.
MINIMUM_SERVER_VERSION = 16

#: Tables that must exist once migrations have run. Checked explicitly rather
#: than trusting "alembic exited 0": a migration can succeed against a database
#: that is not the one we think it is, and a missing table then surfaces much
#: later as a runtime error the user cannot interpret.
REQUIRED_TABLES = (
    "users",
    "resumes",
    "resume_profiles",
    "portals",
    "portal_configs",
    "search_runs",
    "search_run_portals",
    "search_queries",
    "raw_jobs",
    "jobs",
    "job_sources",
    "job_matches",
    "applications",
    "application_status_history",
    "portal_errors",
    "career_page_interactions",
)

#: Key on Alembic's ``Config.attributes`` that names the connection to migrate
#: with. Kept in sync with ``migrations/env.py``; the two are a contract.
MIGRATION_URL_ATTRIBUTE = "sqlalchemy_url"


# --- Exit codes -------------------------------------------------------------
# Distinct per stage so the installer can name the failure rather than saying
# "something went wrong". Kept in sync with the [Code] section of the .iss.

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CANNOT_CONNECT = 2
EXIT_BAD_PASSWORD = 3
EXIT_SERVER_TOO_OLD = 4
EXIT_ROLE_FAILED = 5
EXIT_DATABASE_FAILED = 6
EXIT_MIGRATION_FAILED = 7
EXIT_SCHEMA_INCOMPLETE = 8
EXIT_SEED_FAILED = 9
EXIT_CREDENTIAL_FAILED = 10
EXIT_VERIFY_FAILED = 11
EXIT_UNEXPECTED = 20


class ProvisionError(RuntimeError):
    """A stage failed. Carries the exit code the installer should report."""

    def __init__(self, code: int, message: str, remedy: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.remedy = remedy


@dataclass
class Progress:
    """What this run created, so a failure can undo exactly that much."""

    created_role: bool = False
    created_database: bool = False
    wrote_credential: bool = False
    wrote_env: bool = False
    steps: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.steps.append(message)
        log(message)


_LOG_HANDLE: Any = None


def log(message: str) -> None:
    """Write to the installer log and to stdout.

    stdout is what Inno Setup captures; the file is what support reads later.
    """
    line = message.rstrip()
    print(line, flush=True)
    if _LOG_HANDLE is not None:
        _LOG_HANDLE.write(line + "\n")
        _LOG_HANDLE.flush()


def _open_log() -> None:
    global _LOG_HANDLE
    try:
        paths.ensure_data_tree()
        target = paths.logs_dir() / "install.log"
        _LOG_HANDLE = target.open("a", encoding="utf-8")
        _LOG_HANDLE.write("\n" + "=" * 72 + "\n")
    except OSError:
        # Logging must never be the reason provisioning fails.
        _LOG_HANDLE = None


# --- PostgreSQL -------------------------------------------------------------


async def _connect_admin(password: str, host: str, port: int) -> asyncpg.Connection:
    try:
        return await asyncpg.connect(
            user=ADMIN_USER,
            password=password,
            host=host,
            port=port,
            database=ADMIN_DATABASE,
            timeout=15,
        )
    except asyncpg.InvalidPasswordError as error:
        raise ProvisionError(
            EXIT_BAD_PASSWORD,
            "PostgreSQL did not accept the administrator password.",
            "This is the password chosen when PostgreSQL was installed, not "
            "the Windows password.",
        ) from error
    except (OSError, asyncpg.PostgresError, asyncio.TimeoutError) as error:
        raise ProvisionError(
            EXIT_CANNOT_CONNECT,
            f"Could not reach PostgreSQL at {host}:{port}.",
            "Check that the PostgreSQL service is running and listening on "
            f"port {port}.",
        ) from error


def _quote_literal(value: str) -> str:
    """Quote a string literal for SQL.

    Needed because a role's password cannot be a bind parameter — CREATE ROLE
    is utility syntax, not DML, so the value has to be inlined. The generated
    password is alphanumeric by construction, but this is not the place to
    rely on that.
    """
    return "'" + value.replace("'", "''") + "'"


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def ensure_role(
    connection: asyncpg.Connection, progress: Progress, *, reuse_password: str | None
) -> str:
    """Create the application role, or keep the existing one.

    Returns the password the role now has. On an upgrade where a working
    credential is already stored, the existing password is kept so a rollback
    of this run cannot lock the previous installation out of its own database.
    """
    exists = await connection.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = $1", APP_ROLE
    )

    if exists and reuse_password:
        progress.note(f"Role {APP_ROLE} already exists; keeping its credential.")
        return reuse_password

    password = generate_password()
    try:
        if exists:
            # The role exists but we hold no usable credential for it — the
            # credential file was lost, or belongs to another Windows account.
            # Resetting the password is the only way back in, and it is safe:
            # the role owns no session state.
            await connection.execute(
                f"ALTER ROLE {_quote_ident(APP_ROLE)} WITH LOGIN PASSWORD "
                f"{_quote_literal(password)}"
            )
            progress.note(f"Reset the password for the existing role {APP_ROLE}.")
        else:
            await connection.execute(
                f"CREATE ROLE {_quote_ident(APP_ROLE)} WITH LOGIN PASSWORD "
                f"{_quote_literal(password)} "
                # Least privilege, stated explicitly rather than relying on
                # defaults: this role must never be able to create another
                # database or hand out rights of its own.
                "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION"
            )
            progress.created_role = True
            progress.note(f"Created role {APP_ROLE}.")
    except asyncpg.PostgresError as error:
        raise ProvisionError(
            EXIT_ROLE_FAILED, f"Could not create the application role: {error}"
        ) from error

    return password


async def ensure_database(connection: asyncpg.Connection, progress: Progress) -> bool:
    """Create the database if absent. Returns whether it already existed."""
    exists = await connection.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = $1", APP_DATABASE
    )
    if exists:
        progress.note(f"Database {APP_DATABASE} already exists; keeping it and its data.")
        return True

    try:
        # CREATE DATABASE cannot run inside a transaction block; asyncpg
        # autocommits statements issued outside an explicit transaction.
        #
        # Owned by the administrator, NOT by the application role. That makes a
        # database this tool creates indistinguishable from one that already
        # existed, so both take the same path from here on. The application
        # role is a runtime account and owns nothing anywhere.
        await connection.execute(
            f"CREATE DATABASE {_quote_ident(APP_DATABASE)} ENCODING 'UTF8'"
        )
        progress.created_database = True
        progress.note(f"Created database {APP_DATABASE}.")
    except asyncpg.PostgresError as error:
        raise ProvisionError(
            EXIT_DATABASE_FAILED, f"Could not create the database: {error}"
        ) from error
    return False


async def _connect_admin_to_app_db(
    host: str, port: int, admin_password: str
) -> asyncpg.Connection:
    return await asyncpg.connect(
        user=ADMIN_USER,
        password=admin_password,
        host=host,
        port=port,
        database=APP_DATABASE,
        timeout=15,
    )


async def grant_connect(
    host: str, port: int, admin_password: str, progress: Progress
) -> None:
    """The minimum needed to reach the database at all.

    Split from the table grants because those can only be applied once the
    tables exist, which is after migrations. This runs before.
    """
    connection = await _connect_admin_to_app_db(host, port, admin_password)
    try:
        await connection.execute(
            f"GRANT CONNECT ON DATABASE {_quote_ident(APP_DATABASE)} "
            f"TO {_quote_ident(APP_ROLE)}"
        )
        await connection.execute(f"GRANT USAGE ON SCHEMA public TO {_quote_ident(APP_ROLE)}")

        # Repair a database left in a bad state by an earlier build of this
        # installer, which took ownership of `public` and revoked PUBLIC's
        # access to it. Owning the schema conferred no rights over the tables
        # already inside it, so it achieved nothing except making the change
        # hard to undo. Hand it back to the database owner.
        schema_owner = await connection.fetchval(
            "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'public'"
        )
        database_owner = await connection.fetchval(
            "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = $1",
            APP_DATABASE,
        )
        if schema_owner == APP_ROLE and database_owner != APP_ROLE:
            await connection.execute(
                f"ALTER SCHEMA public OWNER TO {_quote_ident(database_owner)}"
            )
            await connection.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")
            progress.note(
                f"Returned ownership of schema public to {database_owner} "
                "(left by an earlier installer version)."
            )
    except asyncpg.PostgresError as error:
        raise ProvisionError(
            EXIT_DATABASE_FAILED, f"Could not apply database permissions: {error}"
        ) from error
    finally:
        await connection.close()


async def grant_runtime_privileges(
    host: str, port: int, admin_password: str, progress: Progress
) -> None:
    """Give the application role what it needs on the tables — and no more.

    Runs AFTER migrations, deliberately. The tables are owned by the
    administrator, and a privilege cannot be granted on a table that does not
    exist yet; running this first is exactly the bug that made an install over
    an existing database fail with "permission denied for table
    alembic_version".

    Data rights only. No CREATE, no ownership, so the runtime account cannot
    drop a table or alter the schema even if the application is compromised.
    Schema changes arrive only through the installer, which has the
    administrator password.
    """
    connection = await _connect_admin_to_app_db(host, port, admin_password)
    role = _quote_ident(APP_ROLE)
    try:
        await connection.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
        )
        await connection.execute(
            f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {role}"
        )
        # Anything a future migration creates, without needing this to be
        # re-run by hand. The installer re-runs it anyway; this covers the gap.
        await connection.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
        )
        await connection.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {role}"
        )
        granted = await connection.fetchval(
            "SELECT count(DISTINCT table_name) FROM information_schema.table_privileges "
            "WHERE grantee = $1 AND table_schema = 'public'",
            APP_ROLE,
        )
        progress.note(f"Granted data access on {granted} tables to {APP_ROLE}.")
    except asyncpg.PostgresError as error:
        raise ProvisionError(
            EXIT_DATABASE_FAILED, f"Could not grant table permissions: {error}"
        ) from error
    finally:
        await connection.close()


async def server_version(connection: asyncpg.Connection) -> int:
    raw = await connection.fetchval("SHOW server_version_num")
    return int(raw) // 10000


# --- Migrations, schema, seed ----------------------------------------------


def admin_url(admin_password: str, host: str, port: int) -> str:
    """Async URL for the administrator, against the application database."""
    from urllib.parse import quote

    return (
        f"postgresql+asyncpg://{quote(ADMIN_USER, safe='')}:"
        f"{quote(admin_password, safe='')}@{host}:{port}/{APP_DATABASE}"
    )


def run_migrations(url: str, progress: Progress) -> None:
    """Apply every Alembic migration, as the ADMINISTRATOR.

    Not as the application role, and that is the fix for the failure this
    installer hit on a pre-existing database. Migrations are DDL: they create,
    alter and drop tables, and only an object's owner may alter it. In a
    database that already existed, every table — including `alembic_version` —
    is owned by whoever created it, so the application role could not so much
    as read the version table, let alone migrate it.

    Running them as the administrator also means the runtime account never
    needs DDL rights at all, which is a better place to end up than the one
    this was trying to reach. Schema changes arrive through the installer,
    which is the only thing that ever holds the administrator password.

    Saying so is not enough on its own: ``url`` has to survive the trip into
    Alembic, and once did not. See the comment on the attribute below.

    Run in-process rather than by shelling out to the `alembic` command: the
    packaged build has no console scripts and no source tree, so the CLI is
    not available to it.

    Synchronous, and called via ``asyncio.to_thread`` — the project's
    ``migrations/env.py`` drives an async engine with ``asyncio.run()``, which
    raises if a loop is already running on this thread. A worker thread gives
    it the clean slate it expects.
    """
    from alembic import command
    from alembic.config import Config

    script_location = paths.bundle_root() / "migrations"
    if not script_location.is_dir():
        raise ProvisionError(
            EXIT_MIGRATION_FAILED,
            f"The migration scripts are missing from this build ({script_location}).",
        )

    config = Config()
    config.set_main_option("script_location", str(script_location))

    # THIS is what decides the role the migration runs as. It is passed through
    # `attributes` rather than `set_main_option` for two reasons, and both have
    # already cost an installation:
    #
    #   * env.py used to overwrite the ini URL with the one from application
    #     settings, discarding whatever was set here. On an upgrade those
    #     settings resolve to the stored runtime credential, so the DDL ran as
    #     the application role and PostgreSQL refused it.
    #
    #   * `set_main_option` writes into a ConfigParser that interpolates `%`,
    #     and this URL carries a percent-encoded administrator password. A `%`
    #     in that password would raise before a connection was attempted.
    config.attributes[MIGRATION_URL_ATTRIBUTE] = url

    try:
        command.upgrade(config, "head")
    except Exception as error:  # noqa: BLE001 - Alembic raises many types
        raise ProvisionError(
            EXIT_MIGRATION_FAILED, f"A database migration failed: {error}"
        ) from error
    progress.note("Applied all database migrations.")


async def verify_schema(credential: DatabaseCredential, progress: Progress) -> None:
    """Confirm every expected table exists, connecting as the application role."""
    connection = await asyncpg.connect(
        user=credential.username,
        password=credential.password,
        host=credential.host,
        port=credential.port,
        database=credential.database,
        timeout=15,
    )
    try:
        rows = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        present = {row["tablename"] for row in rows}
        missing = [table for table in REQUIRED_TABLES if table not in present]
        if missing:
            raise ProvisionError(
                EXIT_SCHEMA_INCOMPLETE,
                "The database schema is incomplete after migration. Missing: "
                + ", ".join(missing),
            )

        # Listing tables reads the system catalogue, which every role may do —
        # so it proves nothing about whether the application can actually use
        # them. Read each one instead. `alembic_version` is included by name
        # because it is the precise table that failed on a pre-existing
        # database: the schema looked complete while the role could not read a
        # single row of it.
        unreadable: list[str] = []
        for table in ("alembic_version", *REQUIRED_TABLES):
            try:
                await connection.execute(f"SELECT 1 FROM {_quote_ident(table)} LIMIT 1")
            except asyncpg.InsufficientPrivilegeError:
                unreadable.append(table)

        if unreadable:
            raise ProvisionError(
                EXIT_SCHEMA_INCOMPLETE,
                "The application account cannot read these tables: "
                + ", ".join(unreadable)
                + ". The database exists but its permissions are wrong.",
            )

        progress.note(
            f"Verified {len(REQUIRED_TABLES)} tables, all readable by {APP_ROLE}."
        )
    finally:
        await connection.close()


async def seed_reference_data(credential: DatabaseCredential, progress: Progress) -> None:
    """Seed the portal rows and the local user.

    Both are idempotent in the services themselves, so an upgrade re-runs them
    without disturbing a portal the operator has since disabled.
    """
    os.environ["DATABASE_URL"] = credential.sqlalchemy_url

    from app.core.config import get_settings

    get_settings.cache_clear()

    import app.portals.adapters  # noqa: F401 - registers the adapters
    from app.db.session import dispose_engine, get_sessionmaker, init_engine
    from app.services.portal_seed import ensure_portals_seeded
    from app.services.user_service import get_or_create_default_user

    settings = get_settings()
    init_engine(settings)
    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await ensure_portals_seeded(session)
            await get_or_create_default_user(session, settings=settings)
            await session.commit()
        progress.note("Seeded portal and user reference data.")
    except Exception as error:  # noqa: BLE001
        raise ProvisionError(
            EXIT_SEED_FAILED, f"Could not seed reference data: {error}"
        ) from error
    finally:
        await dispose_engine()


# --- Configuration ----------------------------------------------------------


def write_env_file(credential: DatabaseCredential, progress: Progress) -> Path:
    """Generate the application configuration.

    Deliberately carries no password. The connection secret lives only in the
    DPAPI-protected credential file; a plain-text copy here would undo that
    protection for the sake of convenience nobody asked for.
    """
    target = paths.env_file()
    target.parent.mkdir(parents=True, exist_ok=True)

    content = f"""# Generated by the Rising Path Recruiters Job Finding Automation installer. Do not edit.
#
# The database password is NOT here. It is stored separately, encrypted for
# this Windows account, in:
#   {paths.credential_file()}

APP_NAME=Rising Path Recruiters Job Finding Automation
ENVIRONMENT=production
DEBUG=false

API_HOST=127.0.0.1
API_PORT=8100

POSTGRES_HOST={credential.host}
POSTGRES_PORT={credential.port}
POSTGRES_DB={credential.database}
POSTGRES_USER={credential.username}

LOG_LEVEL=INFO
LOG_FORMAT=json

# One process, one port: this server also serves the compiled interface.
SERVE_FRONTEND=true

# The interface is served by the API itself, so there is no second origin and
# cross-origin requests are neither needed nor permitted.
CORS_ALLOW_ORIGINS=
CORS_ALLOW_CREDENTIALS=false

STORAGE_DIR={paths.storage_dir()}
MIN_MATCH_SCORE=70
SINGLE_USER_MODE=true
"""
    target.write_text(content, encoding="utf-8")
    progress.wrote_env = True
    progress.note(f"Wrote configuration to {target}.")
    return target


# --- Rollback ---------------------------------------------------------------


async def rollback(progress: Progress, admin_password: str, host: str, port: int) -> None:
    """Undo only what this run created.

    A database or role that already existed is left completely alone: the
    recruiter's history is worth more than a tidy failure.
    """
    log("Rolling back the changes this run made...")

    if progress.wrote_credential:
        try:
            paths.credential_file().unlink(missing_ok=True)
            log("  removed the credential file")
        except OSError as error:
            log(f"  could not remove the credential file: {error}")

    if progress.wrote_env:
        try:
            paths.env_file().unlink(missing_ok=True)
            log("  removed the generated configuration")
        except OSError as error:
            log(f"  could not remove the configuration: {error}")

    if not (progress.created_database or progress.created_role):
        log("  nothing was created in PostgreSQL; database left untouched")
        return

    try:
        connection = await _connect_admin(admin_password, host, port)
    except ProvisionError:
        log("  could not reconnect to PostgreSQL; database objects left in place")
        return

    try:
        if progress.created_database:
            await connection.execute(
                f"DROP DATABASE IF EXISTS {_quote_ident(APP_DATABASE)} WITH (FORCE)"
            )
            log(f"  dropped the database {APP_DATABASE} that this run created")

        if progress.created_role:
            # A role cannot be dropped while it holds privileges or owns
            # anything, and those references live inside each database rather
            # than globally — so DROP ROLE alone fails with "cannot be dropped
            # because some objects depend on it". DROP OWNED BY, run inside the
            # database where the grants were made, removes them first.
            #
            # Safe against the user's data: this role owns nothing. It was
            # granted rights on tables the administrator owns, and revoking a
            # grant does not touch the table.
            database_present = await connection.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", APP_DATABASE
            )
            if database_present:
                try:
                    scoped = await _connect_admin_to_app_db(host, port, admin_password)
                    try:
                        await scoped.execute(
                            f"DROP OWNED BY {_quote_ident(APP_ROLE)} CASCADE"
                        )
                        log(f"  revoked {APP_ROLE}'s privileges in {APP_DATABASE}")
                    finally:
                        await scoped.close()
                except (asyncpg.PostgresError, OSError) as error:
                    log(f"  could not revoke privileges in {APP_DATABASE}: {error}")

            await connection.execute(f"DROP ROLE IF EXISTS {_quote_ident(APP_ROLE)}")
            log(f"  dropped the role {APP_ROLE} that this run created")
    except asyncpg.PostgresError as error:
        log(f"  rollback could not complete: {error}")
    finally:
        await connection.close()


# --- Orchestration ----------------------------------------------------------


async def provision(admin_password: str, host: str, port: int) -> dict[str, Any]:
    progress = Progress()
    paths.ensure_data_tree()

    # An existing, readable credential means this is an upgrade and the role's
    # password can be preserved.
    reuse_password: str | None = None
    try:
        existing = load_credential()
        if existing.database == APP_DATABASE and existing.username == APP_ROLE:
            reuse_password = existing.password
            progress.note("Found an existing credential for this Windows account.")
    except Exception:  # noqa: BLE001 - absent, unreadable or foreign: all mean "start fresh"
        reuse_password = None

    connection = await _connect_admin(admin_password, host, port)
    try:
        version = await server_version(connection)
        log(f"Connected to PostgreSQL {version}.")
        if version < MINIMUM_SERVER_VERSION:
            raise ProvisionError(
                EXIT_SERVER_TOO_OLD,
                f"This computer has PostgreSQL {version}, but PostgreSQL "
                f"{MINIMUM_SERVER_VERSION} or newer is required.",
            )

        role_password = await ensure_role(
            connection, progress, reuse_password=reuse_password
        )
        database_existed = await ensure_database(connection, progress)
    finally:
        await connection.close()

    credential = DatabaseCredential(
        username=APP_ROLE,
        password=role_password,
        host=host,
        port=port,
        database=APP_DATABASE,
    )

    try:
        # Order matters, and getting it wrong is what broke installing over an
        # existing database:
        #   connect rights  -> migrations (as admin, which owns the tables)
        #   -> table grants (impossible before the tables exist)
        #   -> verify and seed AS THE APPLICATION ROLE, which proves the
        #      grants actually work rather than assuming they do.
        await grant_connect(host, port, admin_password, progress)
        await asyncio.to_thread(
            run_migrations, admin_url(admin_password, host, port), progress
        )
        await grant_runtime_privileges(host, port, admin_password, progress)
        await verify_schema(credential, progress)
        await seed_reference_data(credential, progress)

        save_credential(credential)
        progress.wrote_credential = True
        progress.note(f"Stored the database credential (DPAPI) at {paths.credential_file()}.")

        write_env_file(credential, progress)

        # Final proof: connect exactly the way the application will, using only
        # what was persisted. Everything above could succeed and still leave a
        # configuration the app cannot load.
        await verify_application_can_connect(progress)
    except ProvisionError:
        await rollback(progress, admin_password, host, port)
        raise
    except Exception as error:  # noqa: BLE001
        await rollback(progress, admin_password, host, port)
        raise ProvisionError(EXIT_UNEXPECTED, f"Unexpected failure: {error}") from error

    return {
        "database": APP_DATABASE,
        "role": APP_ROLE,
        "database_existed": database_existed,
        "upgrade": database_existed,
        "steps": progress.steps,
    }


async def verify_application_can_connect(progress: Progress) -> None:
    """Load the stored credential and connect with it, as the app would."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    os.environ.pop("DATABASE_URL", None)

    try:
        credential = load_credential()
        connection = await asyncpg.connect(
            user=credential.username,
            password=credential.password,
            host=credential.host,
            port=credential.port,
            database=credential.database,
            timeout=15,
        )
    except Exception as error:  # noqa: BLE001
        raise ProvisionError(
            EXIT_VERIFY_FAILED,
            f"The application could not connect using the stored credential: {error}",
        ) from error

    try:
        # A real application table, not a catalogue count: this is the last
        # chance to notice that the stored credential connects but cannot read
        # anything, which is exactly how the pre-existing-database failure
        # presented.
        portals = await connection.fetchval("SELECT count(*) FROM portals")
        progress.note(
            f"Application connected with the stored credential and read "
            f"{portals} portal rows."
        )
    except asyncpg.InsufficientPrivilegeError as error:
        raise ProvisionError(
            EXIT_VERIFY_FAILED,
            "The application connected but is not permitted to read its own "
            f"tables: {error}",
        ) from error
    finally:
        await connection.close()


async def check_password(password: str, host: str, port: int) -> None:
    """Verify the administrator password and server version. Creates nothing.

    Used by the installer's password page so a typo is caught where it was
    made, before any file is copied.
    """
    connection = await _connect_admin(password, host, port)
    try:
        version = await server_version(connection)
        log(f"Connected to PostgreSQL {version}.")
        if version < MINIMUM_SERVER_VERSION:
            raise ProvisionError(
                EXIT_SERVER_TOO_OLD,
                f"This computer has PostgreSQL {version}, but PostgreSQL "
                f"{MINIMUM_SERVER_VERSION} or newer is required.",
            )
    finally:
        await connection.close()


def read_password(source: str) -> str:
    """Read the administrator password without putting it on a command line."""
    if source == "-":
        password = sys.stdin.readline()
    else:
        # A file the installer writes and deletes, for the case where stdin is
        # not usable. Consumed and removed immediately.
        path = Path(source)
        password = path.read_text(encoding="utf-8")
        try:
            path.unlink()
        except OSError:
            pass
    return password.rstrip("\r\n")


def main(argv: list[str] | None = None) -> int:
    global APP_DATABASE, APP_ROLE

    parser = argparse.ArgumentParser(
        prog="provision",
        description="Create and migrate this computer's application database.",
    )
    parser.add_argument(
        "--password-from",
        default="-",
        help="'-' to read the administrator password from stdin (default), or "
        "a path to a file that will be deleted after reading.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Override the data directory. The installer passes the location "
        "it configured so both agree.",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit a machine-readable summary."
    )
    parser.add_argument(
        "--test-password",
        action="store_true",
        help="Only check that the administrator password is accepted, then "
        "exit. Creates nothing. Lets the installer validate the password on "
        "the page where it was typed rather than after copying files.",
    )
    # Verification seam. The installer never passes these; they exist so the
    # whole provisioning path can be exercised end to end against a scratch
    # database instead of the one holding a recruiter's real history.
    parser.add_argument("--database", default=APP_DATABASE, help=argparse.SUPPRESS)
    parser.add_argument("--role", default=APP_ROLE, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)

    APP_DATABASE = arguments.database
    APP_ROLE = arguments.role

    if arguments.data_dir:
        os.environ[paths.DATA_DIR_ENV] = arguments.data_dir
        paths.data_dir.cache_clear()

    _open_log()
    log("Rising Path Recruiters Job Finding Automation - database provisioning")

    try:
        password = read_password(arguments.password_from)
    except OSError as error:
        log(f"ERROR: could not read the administrator password ({error}).")
        return EXIT_USAGE

    if not password:
        log("ERROR: no administrator password was supplied.")
        return EXIT_USAGE

    if arguments.test_password:
        try:
            asyncio.run(check_password(password, arguments.host, arguments.port))
        except ProvisionError as error:
            log(f"ERROR [{error.code}]: {error}")
            return error.code
        finally:
            password = ""
        log("The administrator password was accepted.")
        return EXIT_OK

    try:
        result = asyncio.run(provision(password, arguments.host, arguments.port))
    except ProvisionError as error:
        log(f"ERROR [{error.code}]: {error}")
        if error.remedy:
            log(f"WHAT TO DO: {error.remedy}")
        return error.code
    except Exception as error:  # noqa: BLE001
        log(f"ERROR [{EXIT_UNEXPECTED}]: unexpected failure: {error}")
        return EXIT_UNEXPECTED
    finally:
        # The password goes out of scope here and is never persisted.
        password = ""

    if arguments.json:
        print(json.dumps(result))
    log("The application database is ready.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
