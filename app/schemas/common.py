"""The response envelope every endpoint returns.

Success and failure share one shape so the React client has exactly one
parsing path:

    {"success": true, "data": {...}, "message": null, "errors": []}
"""

from __future__ import annotations

import math
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorItem(BaseModel):
    """One error. ``field`` is a dotted path such as ``body.requested_limit``."""

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "code": "validation_error",
            "field": "body.requested_limit",
            "message": "Input should be 20, 50 or 100",
        }
    })

    code: str = Field(description="Stable machine-readable error code.")
    field: str | None = Field(
        default=None,
        description="Dotted path to the offending input, when applicable.",
    )
    message: str = Field(description="Human-readable explanation.")


class ResponseEnvelope(BaseModel, Generic[T]):
    """Standard wrapper for a single-object payload."""

    success: bool = Field(default=True, description="False for any 4xx/5xx.")
    data: T | None = Field(default=None, description="Payload; null on error.")
    message: str | None = Field(
        default=None, description="Optional human-readable summary."
    )
    errors: list[ErrorItem] = Field(
        default_factory=list, description="Empty on success."
    )


class PaginationMeta(BaseModel):
    """Pagination block. Present from Phase 1 so list endpoints added in later
    phases do not change the response contract the frontend was built against.
    """

    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=200)] = 25
    total: Annotated[int, Field(ge=0)] = 0
    total_pages: Annotated[int, Field(ge=0)] = 0
    has_next: bool = False
    has_prev: bool = False

    @classmethod
    def build(cls, *, page: int, page_size: int, total: int) -> PaginationMeta:
        total_pages = math.ceil(total / page_size) if page_size else 0
        return cls(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_prev=page > 1,
        )


class Page(BaseModel, Generic[T]):
    """Payload placed in ``data`` by list endpoints."""

    items: list[T] = Field(default_factory=list)
    pagination: PaginationMeta = Field(default_factory=PaginationMeta)
