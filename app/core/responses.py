"""Helpers for building envelope responses."""

from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.core.exceptions import ErrorDetail


def envelope(
    *,
    success: bool,
    data: Any = None,
    message: str | None = None,
    errors: list[ErrorDetail] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "data": data,
        "message": message,
        "errors": [error.as_dict() for error in (errors or [])],
    }


def error_response(
    *,
    status_code: int,
    errors: list[ErrorDetail],
    message: str | None = None,
) -> JSONResponse:
    """Render a failure envelope. ``data`` is always null on error."""
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            envelope(success=False, data=None, message=message, errors=errors)
        ),
    )


def success_response(
    *,
    data: Any = None,
    message: str | None = None,
    status_code: int = 200,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(envelope(success=True, data=data, message=message)),
    )
