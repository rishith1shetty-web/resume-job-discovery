"""Schemas for the read-only job-processing preview.

The preview exists so Phase 5's decisions can be inspected before anything is
stored: what each field normalised to, which link was chosen as the one to
apply through, and which listings would collapse into a single job.

Nothing in this module implies an application is submitted. The chosen URL is
one the user opens themselves (CLAUDE.md section 1).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import FreshnessWindow


class ProcessingPreviewRequest(BaseModel):
    """Collect a small sample from one portal and show how it would process."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "queries": ["Financial Analyst"],
                "limit": 5,
                "window": "last_7d",
            }
        }
    )

    queries: Annotated[
        list[str],
        Field(
            min_length=1,
            max_length=3,
            description="Same-role title variations to collect for.",
        ),
    ]
    limit: Annotated[int, Field(ge=1, le=10)] = 5
    window: FreshnessWindow = FreshnessWindow.LAST_7D
    location: str | None = None


class DuplicateVerdictOut(BaseModel):
    """Why two listings were, or were not, judged the same vacancy."""

    kind: str = Field(description="hard, probable or none.")
    confidence: float = Field(ge=0.0, le=1.0)
    signals: list[str] = Field(
        default_factory=list, description="Evidence supporting the verdict."
    )
    blockers: list[str] = Field(
        default_factory=list, description="Evidence that prevented a merge."
    )


class ProcessedPreviewJob(BaseModel):
    """One listing after normalisation, with the decisions it would produce."""

    portal_slug: str
    external_id: str | None = None

    title: str = Field(description="Normalised for display; the portal's wording.")
    company: str | None = None
    location: str | None = None
    work_mode: str | None = Field(
        default=None, description="onsite, hybrid or remote. Null when unstated."
    )
    employment_type: str | None = None
    description_chars: int = 0
    posted_at: datetime | None = None

    source_url: str = Field(description="The listing page, normalised.")
    application_url: str | None = None
    preferred_application_url: str = Field(
        description=(
            "The most direct usable link for applying by hand. Preference order "
            "is employer careers page, then applicant tracking system, then the "
            "portal's own listing, then an aggregator redirect."
        )
    )
    preferred_url_kind: str

    canonical_key: str = Field(
        description="Grouping identity. Listings sharing one become one job."
    )
    group_index: int = Field(
        description="Canonical group within this sample; equal values would merge."
    )
    duplicate_of_index: int | None = Field(
        default=None,
        description="Index of the earlier listing in this sample it duplicates.",
    )
    existing_job_id: uuid.UUID | None = Field(
        default=None,
        description="A job already stored that this listing would attach to.",
    )
    verdict: DuplicateVerdictOut | None = None


class RejectedPreviewJob(BaseModel):
    """A capture that could not be normalised, and why."""

    portal_slug: str
    external_id: str | None = None
    title: str | None = None
    reason: str


class ProcessingPreviewResponse(BaseModel):
    """What processing would produce for the collected sample."""

    portal_slug: str
    ok: bool = Field(description="False when the portal refused or failed.")
    collected: int = 0
    canonical_groups: int = Field(
        default=0, description="Distinct jobs the sample would produce."
    )
    duration_ms: int = 0

    jobs: list[ProcessedPreviewJob] = Field(default_factory=list)
    rejected: list[RejectedPreviewJob] = Field(default_factory=list)

    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def from_results(
        cls, *, probe: Any, preview: Any | None
    ) -> ProcessingPreviewResponse:
        if preview is None:
            return cls(
                portal_slug=probe.portal_slug,
                ok=False,
                duration_ms=probe.duration_ms,
                error_code=probe.error_code,
                error_message=probe.error_message,
            )

        return cls(
            portal_slug=probe.portal_slug,
            ok=True,
            collected=len(probe.jobs),
            canonical_groups=preview.canonical_groups,
            duration_ms=probe.duration_ms,
            jobs=[
                ProcessedPreviewJob(
                    portal_slug=job.portal_slug,
                    external_id=job.external_id,
                    title=job.title,
                    company=job.company,
                    location=job.location,
                    work_mode=job.work_mode,
                    employment_type=job.employment_type,
                    description_chars=job.description_chars,
                    posted_at=job.posted_at,
                    source_url=job.source_url,
                    application_url=job.application_url,
                    preferred_application_url=job.preferred_application_url,
                    preferred_url_kind=job.preferred_url_kind,
                    canonical_key=job.canonical_key,
                    group_index=job.group_index,
                    duplicate_of_index=job.duplicate_of_index,
                    existing_job_id=job.existing_job_id,
                    verdict=(
                        DuplicateVerdictOut(**job.verdict) if job.verdict else None
                    ),
                )
                for job in preview.jobs
            ],
            rejected=[
                RejectedPreviewJob(
                    portal_slug=item.portal_slug,
                    external_id=item.external_id,
                    title=item.title,
                    reason=item.reason,
                )
                for item in preview.rejected
            ],
        )
