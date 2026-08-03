"""Job and match response schemas.

Shapes the dashboard columns from CLAUDE.md section 10: title, company,
portal source(s), match score, posted date, location/work mode, status and a
usable URL. Nothing auto-apply related appears here, by design.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import ApplicationStatus, JobUrlKind, WorkMode


class JobSourceOut(BaseModel):
    """One portal that carries this job.

    Every source is kept, not just the winning one: when the same vacancy
    appears on several portals the user still sees which portals had it.
    """

    model_config = ConfigDict(from_attributes=True)

    portal_id: uuid.UUID
    portal_slug: str | None = None
    portal_display_name: str | None = None
    external_id: str | None = None
    source_url: str
    application_url: str | None = None
    url_kind: JobUrlKind
    posted_at: datetime | None = None
    is_preferred: bool = False


class JobMatchOut(BaseModel):
    """How this job scored against one resume.

    Always resume-specific. The same job scored against a different resume is a
    different row and is never shown here.
    """

    model_config = ConfigDict(from_attributes=True)

    final_score: float
    role_score: float
    skill_score: float
    experience_score: float
    responsibility_score: float
    domain_score: float
    education_score: float
    freshness_score: float
    ranking_score: float
    qualified: bool
    rejection_reasons: list[str] = Field(default_factory=list)
    scoring_version: str
    matched_at: datetime


class JobListItem(BaseModel):
    """One row of the results table."""

    id: uuid.UUID
    title: str
    company: str | None = None
    location: str | None = None
    work_mode: WorkMode | None = None
    employment_type: str | None = None

    # Every portal that carries this job, so a merged duplicate still shows
    # both origins.
    portals: list[str] = Field(
        default_factory=list, description="Portal slugs carrying this job."
    )

    posted_at: datetime | None = None
    first_discovered_at: datetime
    last_seen_at: datetime
    expired_at: datetime | None = Field(
        default=None,
        description=(
            "Set when the listing is no longer active. A job condition only - "
            "it never changes the application status."
        ),
    )
    is_expired: bool = False

    # The most direct usable link (employer page, then ATS, then the portal
    # listing). The user opens it and applies manually.
    application_url: str | None = None
    url_kind: JobUrlKind | None = None

    match: JobMatchOut | None = Field(
        default=None, description="Score for the requested resume, when scored."
    )
    application_status: ApplicationStatus = Field(
        default=ApplicationStatus.NOT_APPLIED,
        description="This resume's manual status for this job.",
    )
    applied_at: datetime | None = None


class JobDetail(JobListItem):
    """One job, with its description and every source."""

    description: str | None = None
    normalized_title: str | None = None
    canonical_key: str
    last_verified_at: datetime | None = None
    sources: list[JobSourceOut] = Field(default_factory=list)
