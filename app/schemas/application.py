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
    # No length cap: the column is TEXT, and an arbitrary API limit would
    # reject a note the database would happily store. Omitting the field
    # entirely keeps whatever note is already recorded - notes are replaced
    # only when a replacement is actually sent.
    notes: str | None = Field(
        default=None,
        description=(
            "Optional free text. Omit to keep the existing note; send a value "
            "to replace it."
        ),
    )


class ApplicationOut(BaseModel):
    """One application's current state."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    resume_id: uuid.UUID
    job_id: uuid.UUID
    status: ApplicationStatus
    applied_at: datetime | None = None
    notes: str | None = None
    has_notes: bool = Field(
        default=False,
        description="Whether a note is recorded, for a list-view indicator.",
    )
    created_at: datetime
    updated_at: datetime
    status_changed_at: datetime = Field(
        description=(
            "When the status last actually changed. Derived from the status "
            "history, so it never disagrees with it. Re-sending the same "
            "status does not move it."
        )
    )

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
    """A transition with enough context to render a history row.

    Carries the resume and the *canonical* job. Raw portal captures are never
    exposed here: a listing is an audit record of what a portal returned, not
    something the user applied to.
    """

    resume_id: uuid.UUID
    job_id: uuid.UUID
    job_title: str | None = None
    company: str | None = None


class ApplicationDetailOut(BaseModel):
    """One application together with its full timeline.

    What the dashboard reads when the user opens a job's tracking panel, so a
    status, its note and how it got there arrive in one request.
    """

    application: ApplicationOut | None = Field(
        default=None,
        description=(
            "Null when this resume has recorded nothing for this job yet. "
            "That is a normal state, not an error: treat it as `not_applied`."
        ),
    )
    status: ApplicationStatus = Field(
        description="Current status, or `not_applied` when none is recorded."
    )
    history: list[ApplicationStatusHistoryOut] = Field(default_factory=list)
