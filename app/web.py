"""Serving the built single-page application from the API process.

In development the interface is served by Vite on its own port and talks to the
API across origins. That is the wrong shape for a desktop application: it would
mean shipping Node, running a second process, and explaining a second port to
someone who only wanted to open a program.

So the production build is compiled to static files and served by this same
process. One executable, one port, no Node at run time, and no CORS — the
interface and the API share an origin.

# Why this is an exception handler and not a route

The obvious implementation is a catch-all route, `GET /{path:path}`, returning
`index.html`. It is also wrong, in two ways that only show up later:

  * A catch-all matches *everything*, so any route registered after it is
    unreachable. That silently breaks tests that add routes to a built app,
    and would silently break any future code that mounts a router late.
  * It turns an unknown API path into an HTML page, so a client parsing an
    error response gets markup instead of the error envelope.

Reacting to a 404 instead sidesteps both. Real routes always win because
routing has already finished and failed; API paths keep their JSON envelope
because this defers to the handler that was already registered for them; and
only a genuinely unmatched, non-API GET falls back to the client shell, which
is exactly the case deep-linking needs.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.core import paths
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Prefixes the API owns. A 404 under one of these keeps the JSON envelope, so
#: a mistyped endpoint returns something a client can parse rather than a page
#: it cannot.
API_PREFIXES = ("/api", "/docs", "/redoc", "/openapi.json", "/health")


def _is_api_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in API_PREFIXES)


def mount_frontend(app: FastAPI, dist: Path | None = None) -> bool:
    """Serve the built interface alongside the API.

    Returns whether anything was mounted, so the caller can tell "development,
    as expected" apart from "packaged build is missing its interface" — which
    look identical from the outside and are not the same problem at all.
    """
    root = (dist or paths.frontend_dist()).resolve()
    index = root / "index.html"

    if not index.is_file():
        logger.info("frontend_not_bundled", path=str(root))
        return False

    # Hashed filenames, so these are safe to serve directly and to cache hard.
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    # Whatever already handles HTTP errors — the envelope handler registered by
    # register_exception_handlers. Captured so API 404s still reach it.
    previous = app.exception_handlers.get(StarletteHTTPException)

    @app.exception_handler(StarletteHTTPException)
    async def spa_fallback(request: Request, exc: StarletteHTTPException) -> Response:
        unmatched_page = (
            exc.status_code == 404
            and request.method in ("GET", "HEAD")
            and not _is_api_path(request.url.path)
        )

        if unmatched_page:
            # A real file at the root of the bundle (favicon, manifest…) is
            # served as itself. Resolved and contained: the path comes from the
            # request, and a crafted `../` must not read outside the bundle.
            relative = request.url.path.lstrip("/")
            if relative:
                candidate = (root / relative).resolve()
                if candidate.is_file() and candidate.is_relative_to(root):
                    return FileResponse(candidate)

            # Otherwise the client shell, so the browser can route on the URL.
            return FileResponse(index)

        if previous is not None:
            return await previous(request, exc)
        raise exc

    logger.info("frontend_mounted", path=str(root))
    return True


__all__ = ["API_PREFIXES", "mount_frontend"]
