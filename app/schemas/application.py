"""Manual application tracking schemas.

The user applies through the job URL themselves and records the outcome here.
There is no submit endpoint, no form payload and no apply automation of any
kind - that is permanently out of scope (CLAUDE.md sections 1 and 18).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import ApplicationStatus


class ApplicationUpdateRequest(BaseModel):
    """Set the status the user chose.

    Marking APPLIED stamps ``applied_at`` server-side. Re-sending the status
    the application already has updates nothing and adds no history row.

    There is no EXPIRED value: expiry belongs to the job, and a stale listing
    must never overwrite what the user recorded.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"status": "applied", "notes": "Applied via company site."}
        }
    )

    status: ApplicationStatus
    notes: Annotated[str | None, Field(default=None, max_length=4000)] = None


class ApplicationOut(BaseModel):
    """One application's current state."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    resume_id: uuid.UUID
    job_id: uuid.UUID
    status: ApplicationStatus
    applied_at: datetime | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    # Denormalised for the history table, which would otherwise need a second
    # request per row to be readable.
    job_title: str | None = None
    company: str | None = None
    application_url: str | None = None


class ApplicationStatusHistoryOut(BaseModel):
    """One immutable transition."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    application_id: uuid.UUID
    old_status: ApplicationStatus | None = Field(
        default=None, description="Null for the first record."
    )
    new_status: ApplicationStatus
    changed_at: datetime
    changed_by: str
    note: str | None = None


class ApplicationHistoryItem(ApplicationStatusHistoryOut):
    """A transition with enough job context to render a history row."""

    job_id: uuid.UUID
    job_title: str | None = None
    company: str | None = None
