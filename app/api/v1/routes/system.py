"""Desktop lifecycle control.

Exists for one reason: the packaged application has no console, so there is no
Ctrl-C. `RisingPathRecruitersJobFindingAutomation.exe --shutdown` needs some way to ask the
server to stop, and asking it to stop is meaningfully different from killing
it — an in-flight search finishes its database work and the connection pool is
disposed properly, instead of a write being interrupted halfway.

# Why this is safe to expose

The server binds 127.0.0.1 only, so this is reachable only from this computer,
by this user's session. The worst a local caller can do is stop an application
that same user could stop by closing it. It carries no data and changes no
state.

It is excluded from the OpenAPI schema: it is part of how the desktop wrapper
works, not part of the product's API surface.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from app.core.logging import get_logger
from app.schemas.common import ResponseEnvelope

logger = get_logger(__name__)

router = APIRouter(prefix="/system", tags=["System"])

#: Long enough for the response to reach the caller before the socket closes,
#: short enough that `--shutdown` feels immediate.
_GRACE_SECONDS = 0.5


@router.post(
    "/shutdown",
    include_in_schema=False,
    summary="Stop the local application",
)
async def shutdown(request: Request) -> ResponseEnvelope[dict[str, str]]:
    """Ask the server to stop after this response has been sent."""
    server = getattr(request.app.state, "uvicorn_server", None)

    if server is None:
        # Running under something other than the desktop launcher — the dev
        # server, or the test client. Nothing to stop, and pretending
        # otherwise would be a lie the caller acts on.
        logger.info("shutdown_requested_without_launcher")
        return ResponseEnvelope[dict[str, str]](
            data={"status": "not_managed"},
            message="This process is not managed by the desktop launcher.",
        )

    async def stop_soon() -> None:
        await asyncio.sleep(_GRACE_SECONDS)
        logger.info("shutdown_requested")
        server.should_exit = True

    # Detached deliberately: awaiting it here would deadlock, since the server
    # cannot finish this request while stopping on it.
    asyncio.get_running_loop().create_task(stop_soon())

    return ResponseEnvelope[dict[str, str]](
        data={"status": "stopping"}, message="The application is stopping."
    )
