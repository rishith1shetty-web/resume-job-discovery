"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.portals.adapters  # noqa: F401  - importing populates the registry
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.error_handlers import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.db.session import dispose_engine, init_engine

logger = get_logger(__name__)

DESCRIPTION = """
Resume-based multi-portal job **discovery** and **manual** application
tracking.

This API finds and ranks job listings and records the status a user sets by
hand. It does not apply to jobs: there is no Easy Apply handling, no form
filling, no resume upload automation and no submission of any kind.

All responses use a single envelope:

```json
{"success": true, "data": {}, "message": null, "errors": []}
```

See `docs/FRONTEND_INTEGRATION.md` for base URLs, CORS, pagination and error
conventions.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # Creating the engine does not open a connection, so startup succeeds even
    # when PostgreSQL is down; /health then reports degraded.
    init_engine(settings)
    logger.info(
        "application_started",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
    )
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        # Envelope-wrapped errors, documented once for every endpoint.
        responses={
            422: {"description": "Validation failed. See `errors[]`."},
            500: {"description": "Internal error."},
        },
    )
    app.state.settings = settings

    # Order matters: CORS is added last so it runs outermost and can attach
    # headers to error responses produced further in.
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_allow_origins or settings.cors_allow_origin_regex:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_origin_regex=settings.cors_allow_origin_regex,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            # Let the browser client read the correlation id back.
            expose_headers=[REQUEST_ID_HEADER],
            max_age=600,
        )
    else:
        logger.warning("cors_disabled_no_origins_configured")

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    return app


app = create_app()
