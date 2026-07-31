"""Structured logging via structlog.

Every log line carries the current ``request_id`` so a single API call can be
traced end to end. In later phases the same context is used to bind
``search_run_id`` and ``portal`` to portal-collection logs.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.config import Settings
from app.core.constants import LogFormat

# Bound by RequestContextMiddleware; read by the log processor below.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


def _inject_request_id(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    request_id = request_id_ctx.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


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
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers; drop them so lines aren't duplicated.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers = []
        stdlib_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
