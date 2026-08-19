"""Career page interaction schemas.

Mirrors ``app.schemas.application`` in shape, because the two record the same
kind of thing: a decision the user made and wants remembered. What differs is
what the decision is *about* - a company's recruiting site rather than a
vacancy - so there is no job id, no score and no posted date here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import ApplicationStatus


class CareerPageRef(BaseModel):
    """The career page a decision is about.

    Career pages are discovery output rather than stored records, so the client
    sends the page it is acting on and the server derives the stable key from
    the URL. That key - not the company name, and not the URL as typed - is
    what the decision is stored against, so the same page reached with
    different tracking parameters resolves to one decision.
    """

    company_name: str = Field(min_length=1, max_length=256)
    official_career_url: str = Field(min_length=1, max_length=2048)
    relevant_role: str | None = Field(default=None, max_length=512)
    location: str | None = Field(default=None, max_length=256)


class CareerPageDecisionRequest(CareerPageRef):
    """Mark a career page Applied or Skipped for the active resume.

    Only those two are accepted. A career page has no interview, rejection or
    offer - those are outcomes of applying to a vacancy, and this records that
    the user acted on the *page*.

    Applying to a career page does not apply to the jobs listed on it, and
    skipping one does not skip them. Nothing here writes a job application.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "company_name": "Capital One",
                "official_career_url": "https://www.capitalonecareers.com/",
                "relevant_role": "Financial Analyst",
                "location": "McLean, VA",
                "status": "applied",
            }
        }
    )

    status: ApplicationStatus
    notes: str | None = Field(
        default=None,
        description=(
            "Optional free text. Omit to keep the existing note; send a value "
            "to replace it."
        ),
    )


class CareerPageInteractionOut(BaseModel):
    """One career page decision, as stored."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    resume_id: uuid.UUID
    career_page_key: str = Field(
        description=(
            "Stable identity of the page: its canonicalised official career "
            "URL, hashed. Use it to look the decision up again."
        )
    )
    company_name: str
    official_career_url: str
    relevant_role: str | None = None
    location: str | None = None
    status: ApplicationStatus
    applied_at: datetime | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    #: Fixed discriminator so a merged history list can tell a career page from
    #: a job without inspecting which fields happen to be null.
    kind: str = Field(
        default="career_page",
        description="Always `career_page`. Distinguishes these from job applications.",
    )


class CareerPageStateOut(BaseModel):
    """The decision for one page under one resume, which may be none."""

    interaction: CareerPageInteractionOut | None = None
    status: ApplicationStatus | None = Field(
        default=None,
        description="Null when the user has not decided about this page yet.",
    )
    career_page_key: str


__all__ = [
    "CareerPageDecisionRequest",
    "CareerPageInteractionOut",
    "CareerPageRef",
    "CareerPageStateOut",
]
