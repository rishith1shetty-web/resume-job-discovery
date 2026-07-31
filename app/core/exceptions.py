"""Application exception hierarchy.

Anything raised from these classes is rendered by the central handlers in
``app.core.error_handlers`` into the standard error envelope, so route code
never builds an error response by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus


@dataclass(slots=True)
class ErrorDetail:
    """One machine-readable error.

    ``field`` uses a dotted path (``body.requested_limit``) so the React client
    can bind the message directly to the offending form input.
    """

    code: str
    message: str
    field: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "field": self.field, "message": self.message}


class AppError(Exception):
    """Base class for all expected, client-facing failures.

    Deliberately a plain class rather than a dataclass: ``slots=True``
    dataclasses rebuild the class object, which breaks zero-argument
    ``super()`` in subclasses.
    """

    status_code: int = HTTPStatus.INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: list[ErrorDetail] | None = None,
    ) -> None:
        cls = type(self)
        self.message = message if message is not None else cls.message
        self.status_code = status_code if status_code is not None else cls.status_code
        self.code = code if code is not None else cls.code
        # Default to a single detail mirroring the top-level message, so
        # `errors[]` is never empty on a failure response.
        self.details = details or [ErrorDetail(code=self.code, message=self.message)]
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} code={self.code!r} status={self.status_code}>"


class NotFoundError(AppError):
    status_code = HTTPStatus.NOT_FOUND
    code = "not_found"
    message = "Resource not found."


class ConflictError(AppError):
    status_code = HTTPStatus.CONFLICT
    code = "conflict"
    message = "Resource conflict."


class UnprocessableError(AppError):
    """Well-formed request that violates a business rule."""

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "unprocessable"
    message = "Request could not be processed."


class PayloadTooLargeError(AppError):
    """Upload exceeded the configured size limit."""

    status_code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    code = "payload_too_large"
    message = "Uploaded file is too large."


class UnsupportedMediaTypeError(AppError):
    """File type is not one of the accepted resume formats."""

    status_code = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"
    message = "Unsupported file type."


class UnauthorizedError(AppError):
    status_code = HTTPStatus.UNAUTHORIZED
    code = "unauthorized"
    message = "Authentication required."


class ForbiddenError(AppError):
    status_code = HTTPStatus.FORBIDDEN
    code = "forbidden"
    message = "Operation not permitted."


class ServiceUnavailableError(AppError):
    status_code = HTTPStatus.SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "A required dependency is unavailable."


class DatabaseUnavailableError(ServiceUnavailableError):
    code = "database_unavailable"
    message = "Database is unavailable."
