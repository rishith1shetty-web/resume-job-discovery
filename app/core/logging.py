"""Structured logging via structlog.

Every log line carries the current ``request_id`` so a single API call can be
traced end to end. In later phases the same context is used to bind
``search_run_id`` and ``portal`` to portal-collection logs.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.config import Settings
from app.core.constants import LogFormat

# Bound by RequestContextMiddleware; read by the log processor below.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def _bootstrap_structlog() -> None:
    """Give structlog a safe configuration the moment this module is imported.

    Some modules log while being imported — `app/portals/adapters` registers
    every adapter at import time, and `PortalRegistry.register` logs each one.
    Those calls happen before `configure_logging` can run, because the import
    is what pulls in the module that calls it.

    Unconfigured, structlog answers such a call with its default
    `PrintLoggerFactory`, which writes to `sys.stdout`. In a packaged windowed
    build there is no console, `sys.stdout` is None, and structlog's
    `PrintLogger` does `file or stdout` and then takes a weak reference to it —
    which fails with "cannot create weak reference to 'NoneType' object" and
    kills the application during import, before any error handling exists.

    Routing through the standard library instead makes an early log call
    harmless: a record with no handler is dropped quietly rather than needing a
    stream that may not exist. `configure_logging` replaces this with the real
    configuration as soon as settings are available.
    """
    structlog.configure(
        processors=[structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


_bootstrap_structlog()


def _log_stream() -> Any | None:
    """A stream to log to, or None when the process has no console.

    A packaged windowed build inherits no standard handles, so `sys.stdout` is
    None. `logging.StreamHandler(None)` silently falls back to `sys.stderr`,
    which is None for the same reason, so the handler has to be skipped rather
    than pointed at nothing.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "write"):
            return stream
    return None


def _inject_request_id(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    request_id = request_id_ctx.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


def _rotating_file_handler() -> logging.Handler | None:
    """A log file for the installed application. None in development.

    Development already has a console and a source tree; adding a log file
    there would just be litter. An installed copy has neither, so this is the
    only record of what the backend did.
    """
    from app.core import paths

    if not paths.is_installed():
        return None

    try:
        paths.ensure_data_tree()
        return logging.handlers.RotatingFileHandler(
            paths.logs_dir() / "application.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError:
        # A log file that cannot be opened must not stop the application.
        return None


def configure_logging(settings: Settings) -> None:
    """Configure structlog and route stdlib logging through it.

    Safe to call more than once (tests re-create the app).
    """
    level = logging.getLevelNamesMapping()[settings.log_level]

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _inject_request_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.log_format is LogFormat.JSON
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand off to ProcessorFormatter instead of rendering here. Doing
            # both renders every structlog line twice.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Renders both structlog events and foreign stdlib records (uvicorn,
    # sqlalchemy) through one renderer, so output is uniform.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handlers: list[logging.Handler] = []

    stream = _log_stream()
    if stream is not None:
        stream_handler = logging.StreamHandler(stream)
        stream_handler.setFormatter(formatter)
        handlers.append(stream_handler)

    # An installed copy has no console, so without this the application would
    # produce no log at all — and the one thing support is told to read would
    # be empty. Rolling, so a long-running install cannot fill a disk.
    file_handler = _rotating_file_handler()
    if file_handler is not None:
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    if not handlers:
        # Nowhere to write. Better to drop records than to raise from inside
        # a log call, which would take down whatever was being logged about.
        handlers.append(logging.NullHandler())

    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(level)

    # uvicorn installs its own handlers; drop them so lines aren't duplicated.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers = []
        stdlib_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
