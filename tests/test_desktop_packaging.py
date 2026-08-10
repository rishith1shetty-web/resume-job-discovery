"""The deployment layer: paths, credentials and static serving.

These cover the code that only really runs once the application is installed —
which is exactly the code a source-tree test suite tends not to touch, and
where a mistake costs a reinstall on someone else's computer rather than a
failed assertion here.

One of these tests exists because of a specific bug: mounting the built
interface raised at import time, and nothing caught it until the packaged
application refused to start, because the development tree resolved the built
interface to a directory that never exists. The mount test now builds its own
fixture directory so the path is exercised regardless.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import credentials, paths

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="DPAPI is a Windows facility."
)


# --- Paths ------------------------------------------------------------------


def test_development_writes_inside_the_source_tree(monkeypatch):
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    paths.data_dir.cache_clear()

    assert paths.data_dir() == paths.PROJECT_ROOT
    # The historical location, so an existing checkout keeps its uploads.
    assert paths.storage_dir() == paths.PROJECT_ROOT / "storage"


def test_the_data_directory_can_be_redirected(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    paths.data_dir.cache_clear()

    assert paths.data_dir() == tmp_path.resolve()
    assert paths.config_dir() == tmp_path.resolve() / "config"
    assert paths.logs_dir() == tmp_path.resolve() / "logs"
    assert paths.is_installed()

    paths.data_dir.cache_clear()


def test_an_installed_copy_never_writes_beside_its_executable(monkeypatch, tmp_path):
    """The whole point of the module.

    Program Files is read-only for a standard account. A build that writes
    beside itself fails there — or, worse, succeeds for an administrator and
    silently loses that data for everyone else.
    """
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths.data_dir.cache_clear()

    assert paths.data_dir() == tmp_path / paths.APP_DATA_FOLDER
    assert paths.storage_dir().is_relative_to(tmp_path)
    assert paths.logs_dir().is_relative_to(tmp_path)

    paths.data_dir.cache_clear()


def test_ensure_data_tree_is_repeatable(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path / "tree"))
    paths.data_dir.cache_clear()

    for _ in range(2):
        root = paths.ensure_data_tree()

    assert root.is_dir()
    for child in ("config", "storage", "logs"):
        assert (root / child).is_dir()

    paths.data_dir.cache_clear()


# --- Credentials ------------------------------------------------------------


def test_a_generated_password_is_long_and_url_safe():
    password = credentials.generate_password()

    assert len(password) == 48
    # Alphanumeric by design: this value passes through a PowerShell command
    # line, an Inno Setup exec, a libpq connection string and a SQLAlchemy URL.
    assert password.isalnum()
    assert credentials.generate_password() != password


@WINDOWS_ONLY
def test_the_credential_round_trips(tmp_path):
    original = credentials.DatabaseCredential(
        username="resume_job_discovery_app",
        password=credentials.generate_password(),
        host="127.0.0.1",
        port=5432,
        database="resume_job_discovery",
    )
    target = tmp_path / "credential.dat"

    credentials.save_credential(original, target)

    assert credentials.load_credential(target) == original


@WINDOWS_ONLY
def test_the_password_is_not_recoverable_from_the_file(tmp_path):
    """The reason DPAPI is used at all."""
    credential = credentials.DatabaseCredential(
        "app", "SuperSecretValue123", "127.0.0.1", 5432, "db"
    )
    target = tmp_path / "credential.dat"

    credentials.save_credential(credential, target)
    raw = target.read_bytes()

    assert b"SuperSecretValue123" not in raw
    assert b"app" not in raw
    # It is genuinely encrypted, not merely encoded.
    with pytest.raises(Exception):
        json.loads(raw.decode("utf-8", "ignore"))


@WINDOWS_ONLY
def test_a_missing_credential_is_its_own_condition(tmp_path):
    """Distinct from a corrupt one: the remedy differs."""
    with pytest.raises(credentials.CredentialNotFound):
        credentials.load_credential(tmp_path / "absent.dat")


@WINDOWS_ONLY
def test_a_corrupted_credential_is_reported_not_guessed(tmp_path):
    target = tmp_path / "credential.dat"
    target.write_bytes(b"this is not DPAPI ciphertext")

    with pytest.raises(credentials.CredentialError):
        credentials.load_credential(target)


@WINDOWS_ONLY
def test_the_connection_url_quotes_the_password(tmp_path):
    credential = credentials.DatabaseCredential(
        "app", "p@ss/word:with#chars", "127.0.0.1", 5432, "db"
    )

    url = credential.sqlalchemy_url

    assert url.startswith("postgresql+asyncpg://app:")
    # Unquoted, these would be read as URL syntax rather than as the password.
    assert "p%40ss%2Fword%3Awith%23chars" in url


# --- Static serving ---------------------------------------------------------


@pytest.fixture
def built_interface(tmp_path: Path) -> Path:
    """A minimal stand-in for `npm run build` output."""
    root = tmp_path / "frontend_dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>App</title>", encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    return root


def _app_with_interface(dist: Path) -> FastAPI:
    """An app shaped like the real one: envelope handlers, then the interface.

    Registering the exception handlers matters. The fallback defers to whatever
    already handles HTTP errors so that API 404s keep their envelope, and an
    app without them would test a code path the product never runs.
    """
    from app.core.error_handlers import register_exception_handlers
    from app.web import mount_frontend

    app = FastAPI()

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    register_exception_handlers(app)
    assert mount_frontend(app, dist) is True
    return app


def test_mounting_the_interface_does_not_raise(built_interface):
    """Regression: this raised at import and took the whole app down.

    FastAPI built a response model from the route's `FileResponse |
    JSONResponse` annotation, which is not a valid Pydantic type. The packaged
    application refused to start; nothing in the suite noticed, because the
    development tree resolved the built interface to a path that never exists.
    """
    _app_with_interface(built_interface)


def test_the_shell_is_served_at_the_root(built_interface):
    client = TestClient(_app_with_interface(built_interface))

    response = client.get("/")

    assert response.status_code == 200
    assert "<title>App</title>" in response.text


def test_a_deep_link_returns_the_shell(built_interface):
    """The client routes on the URL, so an unknown path must reach it."""
    client = TestClient(_app_with_interface(built_interface))

    response = client.get("/history")

    assert response.status_code == 200
    assert "<title>App</title>" in response.text


def test_a_real_asset_is_served_rather_than_the_shell(built_interface):
    client = TestClient(_app_with_interface(built_interface))

    response = client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert "console.log" in response.text


def test_an_unknown_api_path_stays_json(built_interface):
    """A client parsing an error must not be handed an HTML page."""
    client = TestClient(_app_with_interface(built_interface))

    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json()["success"] is False


def test_a_route_added_after_mounting_still_wins(built_interface):
    """Regression: a catch-all route made everything registered later dead.

    The first implementation served the shell from `GET /{path:path}`, which
    matches every path — so any route added afterwards was unreachable, and
    five error-handling tests started returning 404 instead of the status they
    asserted. Reacting to a 404 instead means routing finishes first and real
    routes always win.
    """
    app = _app_with_interface(built_interface)

    @app.get("/api/v1/_added/later")
    async def added_later() -> dict[str, bool]:
        return {"reached": True}

    client = TestClient(app)

    assert client.get("/api/v1/_added/later").json() == {"reached": True}


def test_a_page_route_added_after_mounting_still_wins(built_interface):
    """The same guarantee for a non-API path, which is the shell's own case."""
    app = _app_with_interface(built_interface)

    @app.get("/reports")
    async def reports() -> dict[str, bool]:
        return {"reached": True}

    client = TestClient(app)

    assert client.get("/reports").json() == {"reached": True}


def test_a_non_get_unknown_path_is_not_given_the_shell(built_interface):
    """A POST to nowhere is a client error, not a page navigation."""
    client = TestClient(_app_with_interface(built_interface))

    response = client.post("/not-a-route")

    assert "<title>App</title>" not in response.text


def test_the_interface_is_off_by_default_in_development(monkeypatch):
    """The API's contract must not depend on whether someone ran a build."""
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "is_frozen", lambda: False)

    from app.core.config import Settings

    assert Settings(_env_file=None).serve_frontend is False


def test_the_api_still_answers_with_the_interface_mounted(built_interface):
    """The catch-all must not shadow a real route."""
    client = TestClient(_app_with_interface(built_interface))

    assert client.get("/api/v1/health").json() == {"status": "ok"}


def test_nothing_is_mounted_without_a_build(tmp_path):
    """A development checkout is the normal case, not a failure."""
    from app.web import mount_frontend

    assert mount_frontend(FastAPI(), tmp_path / "never-built") is False


def test_a_traversal_cannot_escape_the_bundle(built_interface, tmp_path):
    """`full_path` comes from the request; `../` must not read outside."""
    secret = tmp_path / "secret.txt"
    secret.write_text("credentials", encoding="utf-8")
    client = TestClient(_app_with_interface(built_interface))

    response = client.get("/../secret.txt")

    # Either refused outright or answered with the shell — never the file.
    assert "credentials" not in response.text


# --- Provisioning contract --------------------------------------------------


def test_the_installer_and_the_app_agree_on_the_table_list():
    """Provisioning verifies a hard-coded list; drift would ship a bad check."""
    sys.path.insert(0, str(paths.PROJECT_ROOT / "installer"))
    try:
        import provision
    finally:
        sys.path.pop(0)

    from app.models import Base

    declared = set(provision.REQUIRED_TABLES)
    actual = set(Base.metadata.tables)

    missing = declared - actual
    assert not missing, f"Provisioning verifies tables that do not exist: {missing}"


def test_every_provisioning_exit_code_is_distinct():
    """The installer maps each to a different sentence."""
    sys.path.insert(0, str(paths.PROJECT_ROOT / "installer"))
    try:
        import provision
    finally:
        sys.path.pop(0)

    codes = {
        name: value
        for name, value in vars(provision).items()
        if name.startswith("EXIT_") and isinstance(value, int)
    }

    assert len(set(codes.values())) == len(codes), f"Duplicate exit codes: {codes}"


# --- Logging in a windowed, frozen process ----------------------------------
#
# A packaged windowed build launched from a shortcut inherits no standard
# handles, so `sys.stdout` and `sys.stderr` are None. These tests reproduce
# that condition directly, because it is not reachable from a normal test run
# and was not reachable from the way the frozen build was first verified: a
# GUI-subsystem executable started from a shell *with piped output* inherits
# those pipes and has real streams, which hid the fault completely.


@pytest.fixture
def no_console(monkeypatch):
    """Exactly what a windowed frozen process sees."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)


#: Reproduces the packaged startup in a *fresh interpreter*.
#:
#: A subprocess is the only faithful way to test this. The failure needs
#: structlog to be unconfigured at the moment of the first log call, and by the
#: time any test runs, something in the session has already configured it — so
#: an in-process version of this test passes whether the bug is present or not.
#: A new interpreter with no standard streams is exactly the installed
#: application's situation.
_FROZEN_STARTUP_PROBE = """
import sys
sys.stdout = None
sys.stderr = None

# The import chain the launcher performs. `app.portals.adapters` registers
# every adapter at import time and the registry logs each one, which is the
# call that crashed before configure_logging could ever run.
import app.portals.adapters
from app.main import create_app

create_app()

import os
os.write(1, b"") if False else None
"""


def test_the_packaged_startup_path_survives_having_no_console(tmp_path):
    """Regression: the installed application died during import.

    A windowed build launched from a shortcut inherits no standard handles, so
    `sys.stdout` is None. Unconfigured structlog answers a log call with a
    logger that does `file or sys.stdout` and then takes a weak reference to
    the result, which raises

        TypeError: cannot create weak reference to 'NoneType' object

    That happened while `app.portals.adapters` was still being imported, long
    before any error handling existed to explain it.
    """
    probe = tmp_path / "frozen_startup_probe.py"
    probe.write_text(_FROZEN_STARTUP_PROBE, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(probe)],
        cwd=paths.PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert "cannot create weak reference" not in result.stderr, (
        "The no-console startup path regressed:\n" + result.stderr
    )
    assert result.returncode == 0, (
        f"Startup without a console failed (exit {result.returncode}):\n{result.stderr}"
    )


def test_configure_logging_does_not_require_a_stream(no_console, monkeypatch, tmp_path):
    """`StreamHandler(None)` silently falls back to sys.stderr — also None."""
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    paths.data_dir.cache_clear()

    from app.core.config import Settings
    from app.core.logging import configure_logging, get_logger

    # log_level is passed explicitly: the suite sets it to WARNING in the
    # environment, and a test that silently depends on that reports a filtered
    # record as a missing one.
    configure_logging(Settings(_env_file=None, log_level="INFO"))
    get_logger("regression.configured").info("after_configuration", value=1)

    # Installed and consoleless: the file is the only record there is.
    written = (tmp_path / "logs" / "application.log").read_text(encoding="utf-8")
    assert "after_configuration" in written

    paths.data_dir.cache_clear()


def test_configure_logging_still_uses_the_console_when_there_is_one(monkeypatch, capsys):
    """Development behaviour must not change."""
    monkeypatch.delenv(paths.DATA_DIR_ENV, raising=False)
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    paths.data_dir.cache_clear()

    from app.core.config import Settings
    from app.core.logging import configure_logging, get_logger

    configure_logging(Settings(_env_file=None, log_level="INFO"))
    get_logger("regression.console").info("visible_in_development")

    assert "visible_in_development" in capsys.readouterr().out
    # And no file is created beside the source tree in development.
    assert not (paths.PROJECT_ROOT / "logs" / "application.log").exists()

    paths.data_dir.cache_clear()


def test_the_launcher_replaces_missing_streams_before_importing_the_app():
    """The launcher must repair the environment before any app import.

    Third-party code makes the same assumption structlog did, and a crash at
    this point happens before there is any way to report it.
    """
    import inspect

    source = (paths.PROJECT_ROOT / "desktop" / "launcher.py").read_text(encoding="utf-8")

    guard_at = source.index("_ensure_standard_streams()")
    app_import_at = source.index("from app.core import paths")
    assert guard_at < app_import_at, (
        "Standard streams must be restored before the first app import."
    )
    assert "os.devnull" in source


def _provision_module():
    sys.path.insert(0, str(paths.PROJECT_ROOT / "installer"))
    try:
        import provision

        return provision
    finally:
        sys.path.pop(0)


def test_migrations_run_as_the_administrator_not_the_application_role():
    """Regression: installing over an existing database failed here.

    Migrations are DDL, and only an object's owner may alter it. In a database
    that already existed every table — including `alembic_version` — belongs to
    whoever created it, so running migrations as the application role failed
    with "permission denied for table alembic_version" before it could read the
    current version.

    The signature is the guard: `run_migrations` takes a URL built for the
    administrator. Passing it an application credential is the mistake this
    exists to prevent.
    """
    import inspect

    provision = _provision_module()

    parameters = list(inspect.signature(provision.run_migrations).parameters)
    assert parameters[0] == "url", (
        "run_migrations must take an explicit URL. It previously took the "
        "application credential, which is what caused the failure."
    )
    assert "ADMIN_USER" in inspect.getsource(provision.admin_url)


def test_the_runtime_role_is_never_given_ownership():
    """The application account is a data account, not an owner.

    Owning the schema conferred no rights over tables already inside it, so it
    bought nothing — while making the change hard to undo and giving the
    runtime account the ability to drop its own tables.
    """
    import inspect

    provision = _provision_module()

    creation = inspect.getsource(provision.ensure_database)
    assert "OWNER" not in creation, (
        "A database this tool creates must be owned by the administrator, so "
        "that a fresh database and a pre-existing one take the same path."
    )

    grants = inspect.getsource(provision.grant_runtime_privileges)
    assert "ALTER SCHEMA" not in grants
    assert "GRANT ALL" not in grants
    # Data rights only.
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert privilege in grants


def test_table_grants_are_applied_after_migrations():
    """A privilege cannot be granted on a table that does not exist yet."""
    import inspect

    provision = _provision_module()
    body = inspect.getsource(provision.provision)

    migrate_at = body.index("run_migrations")
    grant_at = body.index("grant_runtime_privileges")
    assert migrate_at < grant_at, (
        "Table grants must follow migrations; granting first is what left the "
        "application role with no privileges on an existing database."
    )


def test_rollback_clears_privileges_before_dropping_the_role():
    """Regression: `DROP ROLE` failed while the role still held grants.

    PostgreSQL refuses to drop a role that anything depends on, and those
    dependencies live inside each database rather than globally.
    """
    import inspect

    provision = _provision_module()
    body = inspect.getsource(provision.rollback)

    # Matched on the executed SQL, not on prose: the explanatory comment above
    # the fix also mentions DROP ROLE, and matching that would pass regardless
    # of what the code does.
    assert "DROP OWNED BY" in body
    drop_owned_at = body.index('f"DROP OWNED BY')
    drop_role_at = body.index('f"DROP ROLE IF EXISTS')
    assert drop_owned_at < drop_role_at


def test_verification_reads_tables_rather_than_listing_them():
    """Listing tables reads the catalogue, which proves nothing about access.

    The failed install had a complete-looking schema that the application role
    could not read a single row of.
    """
    import inspect

    provision = _provision_module()
    body = inspect.getsource(provision.verify_schema)

    assert "alembic_version" in body
    assert "InsufficientPrivilege" in body


def test_the_generated_configuration_carries_no_password(tmp_path, monkeypatch):
    """The credential is DPAPI-protected; a plain copy would undo that."""
    sys.path.insert(0, str(paths.PROJECT_ROOT / "installer"))
    try:
        import provision
    finally:
        sys.path.pop(0)

    monkeypatch.setenv(paths.DATA_DIR_ENV, str(tmp_path))
    paths.data_dir.cache_clear()

    credential = credentials.DatabaseCredential(
        "app_role", "TopSecretPassword", "127.0.0.1", 5432, "resume_job_discovery"
    )
    written = provision.write_env_file(credential, provision.Progress())
    content = written.read_text(encoding="utf-8")

    assert "TopSecretPassword" not in content
    assert "POSTGRES_USER=app_role" in content
    assert "POSTGRES_PASSWORD" not in content

    paths.data_dir.cache_clear()


# --- Interface API base URL -------------------------------------------------
#
# The packaged application serves the interface and the API from one origin, so
# the interface calls the API at a relative base. That base has to survive URL
# construction, and once did not: `new URL()` given a single relative string
# throws, so every call failed before a request was sent and the interface
# blamed the backend for being unreachable.


def _client_source() -> str:
    return (paths.PROJECT_ROOT / "frontend" / "src" / "api" / "client.js").read_text(
        encoding="utf-8"
    )


def _production_api_base() -> str:
    env = paths.PROJECT_ROOT / "frontend" / ".env.production"
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "VITE_API_BASE_URL":
            return value.strip()
    raise AssertionError("VITE_API_BASE_URL is not set in frontend/.env.production")


def test_the_packaged_interface_calls_the_api_on_its_own_origin():
    """A baked host or port would point the installed copy somewhere else.

    Port 8000 in particular belongs to a different application on these
    machines, so a stale value would not fail loudly — it would reach the
    wrong server.
    """
    base = _production_api_base()

    assert base.startswith("/"), f"expected a same-origin base, got {base!r}"
    assert "://" not in base
    assert "8000" not in base


def test_url_building_accepts_a_relative_base():
    """The regression itself: a relative base must survive `new URL()`."""
    source = _client_source()
    # Anchored on the assignment, not on "new URL(", which also appears in the
    # comment explaining this very requirement.
    call = source[source.index("const url = new URL(") :].split("\n", 1)[0]

    assert "window.location.origin" in call, (
        "buildUrl must pass a base to new URL(), or a relative "
        f"VITE_API_BASE_URL throws TypeError: Invalid URL. Found: {call!r}"
    )


def test_a_transport_failure_is_the_only_thing_reported_as_one():
    """Building the URL belongs outside the try, or its errors are mislabelled.

    A malformed base reported as "is the backend running?" sent a whole round
    of diagnosis at a server that had never been contacted.
    """
    source = _client_source()
    body = source[source.index("export async function requestEnvelope") :]
    body = body[: body.index("\n}\n")]

    built_at = body.index("buildUrl(path, query)")
    reported_at = body.index("isNetworkError: true")

    assert built_at < body.index("await fetch("), (
        "buildUrl must run before the try block that reports network failures, "
        "so a malformed URL is not reported as an unreachable server"
    )
    assert built_at < reported_at
