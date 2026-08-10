"""The program the recruiter actually runs.

# What it is

One executable that starts the API in-process, waits until it is genuinely
answering, and opens the browser at it. There is no Vite, no Node, no second
process and no console window: the interface is compiled to static files and
served by this same server (see `app/web.py`), so a desktop launch is one
process listening on one loopback port.

# Design decisions worth stating

**In-process, not a subprocess.** Spawning uvicorn as a child would mean
managing a process whose lifetime is not tied to this one — the classic way to
leave an orphaned server holding a port after a crash. Running the server on a
thread of this process means the operating system reclaims it no matter how
this program ends.

**Bound to 127.0.0.1, never 0.0.0.0.** This is a single-user desktop
application holding someone's resumes and application history. It has no
authentication because it is not reachable from the network, and that
assumption only holds while the bind address does.

**Port 8100, never 8000.** Port 8000 belongs to the separate LinkedIn/Dice
application that may be installed on the same computer. Binding it would break
that product; this refuses to start rather than take it.

**Failure is explained, not raised.** There is no console to print a traceback
to, so every failure this can anticipate becomes a message box with a cause and
a remedy, and the detail goes to the log.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _ensure_standard_streams() -> None:
    """Guarantee `sys.stdout` and `sys.stderr` exist before anything imports.

    This is a windowed build: a GUI-subsystem process launched from a shortcut
    inherits no standard handles, so Python sets both to None. Most code never
    notices, but any library that writes to them — or merely *refers* to them —
    fails in ways that have nothing to do with what it was doing.

    structlog is the case that bit us. Its `PrintLogger` does
    `file or sys.stdout` and then takes a weak reference to the result, so a
    single log line emitted during import died with "cannot create weak
    reference to 'NoneType' object" and took the whole application with it,
    before any error handling was in place to explain it.

    `app/core/logging.py` no longer depends on a stream, so this is no longer
    load-bearing for our own code. It stays because third-party code makes the
    same assumption and we cannot audit all of it — and because a crash here
    happens before there is any way to report it.

    Deliberately a real file, not `os.devnull`: anything a library prints
    directly is diagnostic information, and on a machine with no console it
    would otherwise be lost exactly when it is most needed.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return

    sink = None
    try:
        from app.core import paths as _paths

        _paths.ensure_data_tree()
        sink = (_paths.logs_dir() / "stdout.log").open(
            "a", encoding="utf-8", errors="replace", buffering=1
        )
    except Exception:  # noqa: BLE001 - fall back rather than fail to start
        try:
            import os

            sink = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return

    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


_ensure_standard_streams()

from app.core import paths  # noqa: E402

APP_TITLE = "Rising Path Recruiters Job Finding Automation"

#: Deliberately not 8000. See the module docstring.
DEFAULT_PORT = 8100
HOST = "127.0.0.1"

#: Names a machine-wide object, so a second launch finds the first rather than
#: starting a competing server.
_MUTEX_NAME = "Global\\RisingPathRecruitersJobFindingAutomation.Launcher"

#: How long the server is given to start answering before we conclude it will
#: not. Generous: a cold start on a loaded machine with a slow disk is not a
#: failure, and a false negative here is a support call.
STARTUP_TIMEOUT_SECONDS = 90

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 0  # not an error: the window was brought forward
EXIT_NOT_PROVISIONED = 2
EXIT_PORT_IN_USE = 3
EXIT_STARTUP_TIMEOUT = 4
EXIT_CONFIG_BROKEN = 5
EXIT_UNEXPECTED = 20

logger = logging.getLogger("launcher")


# --- Logging ----------------------------------------------------------------


def configure_logging() -> Path:
    """Rolling file log. Nothing is written beside the executable.

    Five 2 MB files: enough to cover a support conversation, bounded so an
    application left running for months cannot fill a disk.
    """
    paths.ensure_data_tree()
    target = paths.logs_dir() / "launcher.log"

    handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    # A console only exists when run from a terminal for diagnosis; the
    # windowed build has none, and this is harmless there.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
        logger.addHandler(stream)

    return target


def show_error(message: str, detail: str = "") -> None:
    """Report a failure to someone who has no console.

    The message is what to do about it; the detail goes to the log. A stack
    trace in a dialog tells a recruiter nothing and looks like a crash.
    """
    logger.error("%s %s", message, detail)
    body = message
    if detail:
        body += f"\n\nTechnical detail has been written to:\n{paths.logs_dir() / 'launcher.log'}"
    try:
        # MB_ICONERROR | MB_OK | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, body, f"{APP_TITLE}", 0x10 | 0x0 | 0x10000)
    except Exception:  # noqa: BLE001 - never let reporting a failure fail
        print(body, file=sys.stderr)


# --- Single instance --------------------------------------------------------


class SingleInstance:
    """A named mutex held for the process lifetime."""

    def __init__(self) -> None:
        self._handle = None
        self.already_running = False

    def acquire(self) -> bool:
        ERROR_ALREADY_EXISTS = 183
        self._handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        self.already_running = ctypes.GetLastError() == ERROR_ALREADY_EXISTS
        return not self.already_running

    def release(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.ReleaseMutex(self._handle)
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


# --- Health -----------------------------------------------------------------


def port_is_free(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def health_url(port: int) -> str:
    return f"http://{HOST}:{port}/api/v1/health"


def app_url(port: int) -> str:
    return f"http://{HOST}:{port}/"


def wait_for_health(port: int, timeout: int = STARTUP_TIMEOUT_SECONDS) -> bool:
    """Poll until the API answers.

    Any HTTP reply counts as started, including 503. A degraded database is a
    condition the interface itself explains far better than a dialog can; only
    silence means the server never came up.
    """
    deadline = time.monotonic() + timeout
    url = health_url(port)
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.4)
    return False


def is_already_serving(port: int) -> bool:
    try:
        with urllib.request.urlopen(health_url(port), timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001
        return False


# --- Preflight --------------------------------------------------------------


def preflight() -> tuple[bool, str, str, int]:
    """Confirm this installation is usable before starting anything.

    Returns ``(ok, message, detail, exit_code)``.
    """
    from app.core.credentials import (
        CredentialError,
        CredentialNotFound,
        CredentialNotOurs,
        load_credential,
    )

    try:
        load_credential()
    except CredentialNotFound as error:
        return (
            False,
            "This application has not been set up on this computer yet.\n\n"
            "Run the installer again to create its database.",
            str(error),
            EXIT_NOT_PROVISIONED,
        )
    except CredentialNotOurs as error:
        return (
            False,
            "The database credential belongs to a different Windows account.\n\n"
            "Sign in as the account the application was installed for, or run "
            "the installer again while signed in as this account.",
            str(error),
            EXIT_NOT_PROVISIONED,
        )
    except CredentialError as error:
        return (
            False,
            "The stored database credential could not be read.\n\n"
            "Run the installer again to recreate it.",
            str(error),
            EXIT_CONFIG_BROKEN,
        )

    try:
        from app.core.config import get_settings

        get_settings.cache_clear()
        get_settings()
    except Exception as error:  # noqa: BLE001
        return (
            False,
            "The application configuration is missing or invalid.\n\n"
            "Run the installer again to regenerate it.",
            str(error),
            EXIT_CONFIG_BROKEN,
        )

    return True, "", "", EXIT_OK


def run_check(port: int) -> int:
    """`--check`: report whether this installation is in working order.

    Exit code 0 means configuration, credential, database and schema are all
    fine. Intended for support, and safe to run while the app is closed.
    """
    ok, message, detail, code = preflight()
    if not ok:
        print(f"FAIL: {message}")
        print(f"      {detail}")
        return code

    import asyncio

    import asyncpg

    from app.core.credentials import load_credential

    credential = load_credential()
    print(f"Configuration   OK   {paths.env_file()}")
    print(f"Credential      OK   {credential.username}@{credential.host}:{credential.port}")

    async def probe() -> tuple[bool, str, int]:
        try:
            connection = await asyncpg.connect(
                user=credential.username,
                password=credential.password,
                host=credential.host,
                port=credential.port,
                database=credential.database,
                timeout=10,
            )
        except Exception as error:  # noqa: BLE001
            return False, str(error), 0
        try:
            count = await connection.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
            )
            return True, "", int(count)
        finally:
            await connection.close()

    reachable, error, tables = asyncio.run(probe())
    if not reachable:
        print(f"Database        FAIL {error}")
        return EXIT_CONFIG_BROKEN

    print(f"Database        OK   {credential.database} ({tables} tables)")
    print(f"Data directory  OK   {paths.data_dir()}")
    print(f"Interface       {'OK  ' if paths.frontend_dist().is_dir() else 'MISS'} "
          f"{paths.frontend_dist()}")
    print(f"Port {port:<11}{'free' if port_is_free(port) else 'in use'}")
    return EXIT_OK


def run_shutdown(port: int) -> int:
    """`--shutdown`: stop a running instance.

    Implemented by asking the server to stop rather than by killing a process
    id, so an in-flight search finishes its database work instead of being
    interrupted mid-write.
    """
    if not is_already_serving(port):
        print("The application is not running.")
        return EXIT_OK
    try:
        request = urllib.request.Request(
            f"http://{HOST}:{port}/api/v1/system/shutdown", method="POST"
        )
        with urllib.request.urlopen(request, timeout=10):
            pass
    except Exception as error:  # noqa: BLE001
        print(f"Could not stop the application: {error}")
        return EXIT_UNEXPECTED
    print("The application is stopping.")
    return EXIT_OK


# --- Server -----------------------------------------------------------------


def serve(port: int, open_browser: bool) -> int:
    """Start the API and hold the process open until it stops."""
    import uvicorn

    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    settings = get_settings()

    if port == 8000:
        show_error(
            "Port 8000 is reserved for a different application and will not be used.",
            "Refusing to bind 8000.",
        )
        return EXIT_PORT_IN_USE

    if not port_is_free(port):
        if is_already_serving(port):
            logger.info("already serving on %s; opening the browser", port)
            if open_browser:
                webbrowser.open(app_url(port))
            return EXIT_ALREADY_RUNNING
        show_error(
            f"Port {port} is already being used by another program.\n\n"
            "Close whatever is using it and start the application again.",
            f"bind {HOST}:{port} failed",
        )
        return EXIT_PORT_IN_USE

    application = create_app(settings)
    config = uvicorn.Config(
        application,
        host=HOST,
        port=port,
        log_level=settings.log_level.lower(),
        access_log=False,
        # The launcher owns process lifetime; uvicorn installing its own
        # handlers would fight the shutdown endpoint.
        lifespan="on",
    )
    server = uvicorn.Server(config)
    application.state.uvicorn_server = server

    def open_when_ready() -> None:
        if wait_for_health(port):
            logger.info("backend healthy on %s", port)
            if open_browser:
                webbrowser.open(app_url(port))
        else:
            show_error(
                "The application did not finish starting.\n\n"
                "Check that PostgreSQL is running, then try again.",
                f"no response on {health_url(port)} within {STARTUP_TIMEOUT_SECONDS}s",
            )
            server.should_exit = True

    threading.Thread(target=open_when_ready, name="startup-probe", daemon=True).start()

    logger.info("starting server on %s:%s", HOST, port)
    server.run()
    logger.info("server stopped")
    return EXIT_OK


# --- Entry point ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="RisingPathRecruitersJobFindingAutomation", add_help=True)
    parser.add_argument("--check", action="store_true", help="Verify this installation.")
    parser.add_argument("--shutdown", action="store_true", help="Stop a running instance.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--version", action="store_true")
    arguments = parser.parse_args(argv)

    log_path = configure_logging()

    if arguments.version:
        from app.core.config import get_settings

        print(f"{APP_TITLE} {get_settings().app_version}")
        return EXIT_OK

    if arguments.shutdown:
        return run_shutdown(arguments.port)

    if arguments.check:
        return run_check(arguments.port)

    logger.info("launcher starting; data directory %s; log %s", paths.data_dir(), log_path)

    # A second launch must not start a second server. The mutex is checked
    # before anything is bound so the race is decided by the OS, not by timing.
    instance = SingleInstance()
    if not instance.acquire():
        logger.info("another instance is already running")
        if not arguments.no_browser:
            webbrowser.open(app_url(arguments.port))
        return EXIT_ALREADY_RUNNING

    try:
        ok, message, detail, code = preflight()
        if not ok:
            show_error(message, detail)
            return code
        return serve(arguments.port, open_browser=not arguments.no_browser)
    except Exception as error:  # noqa: BLE001
        logger.exception("unhandled launcher failure")
        show_error(
            "The application could not start.\n\n"
            "Please send the log file to support.",
            str(error),
        )
        return EXIT_UNEXPECTED
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
