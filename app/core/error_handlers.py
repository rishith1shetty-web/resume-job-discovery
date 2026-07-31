"""Central exception handling.

Registered once in the app factory. No route builds an error response itself,
which is what keeps the envelope contract stable for the React client.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError, ErrorDetail
from app.core.logging import get_logger
from app.core.responses import error_response

logger = get_logger(__name__)

_GENERIC_500_MESSAGE = "An internal error occurred. Please try again."


def _format_location(location: tuple[Any, ...]) -> str | None:
    """Turn a pydantic ``loc`` tuple into a dotted path.

    ``("body", "portals", 0, "id")`` -> ``"body.portals.0.id"``
    """
    if not location:
        return None
    return ".".join(str(part) for part in location)


def _validation_details(exc: RequestValidationError | PydanticValidationError) -> list[ErrorDetail]:
    details: list[ErrorDetail] = []
    for error in exc.errors():
        details.append(
            ErrorDetail(
                code=error.get("type", "validation_error"),
                field=_format_location(tuple(error.get("loc", ()))),
                message=error.get("msg", "Invalid value."),
            )
        )
    return details or [ErrorDetail(code="validation_error", message="Invalid request.")]


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        # Expected failures: log at warning, no stack trace.
        logger.warning(
            "app_error", code=exc.code, status_code=exc.status_code, message=exc.message
        )
        return error_response(
            status_code=exc.status_code, errors=exc.details, message=exc.message
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = _validation_details(exc)
        logger.info("request_validation_failed", error_count=len(details))
        return error_response(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            errors=details,
            message="Validation failed.",
        )

    @app.exception_handler(PydanticValidationError)
    async def handle_response_validation(
        _request: Request, exc: PydanticValidationError
    ) -> JSONResponse:
        # A response failing its own schema is a server bug, not a client error.
        logger.error("response_validation_failed", errors=exc.errors())
        return error_response(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            errors=[ErrorDetail(code="internal_error", message=_GENERIC_500_MESSAGE)],
            message=_GENERIC_500_MESSAGE,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Covers 404/405 raised by the router itself.
        try:
            phrase = HTTPStatus(exc.status_code).phrase
            code = phrase.lower().replace(" ", "_")
        except ValueError:
            code = "http_error"
        message = str(exc.detail) if exc.detail else code.replace("_", " ").capitalize()
        return error_response(
            status_code=exc.status_code,
            errors=[ErrorDetail(code=code, message=message)],
            message=message,
        )

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(
        _request: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        # Never leak SQL or connection strings to the client.
        logger.error("database_error", error_type=type(exc).__name__, exc_info=exc)
        message = "A database error occurred."
        return error_response(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            errors=[ErrorDetail(code="database_error", message=message)],
            message=message,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled_exception", error_type=type(exc).__name__, exc_info=exc)
        return error_response(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            errors=[ErrorDetail(code="internal_error", message=_GENERIC_500_MESSAGE)],
            message=_GENERIC_500_MESSAGE,
        )
