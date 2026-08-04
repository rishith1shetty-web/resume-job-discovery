"""Portal read schemas.

Read-only in Phase 3. The dashboard needs the portal checkboxes to create a
search run, so ``GET /portals`` exists; enabling, disabling and configuring
portals belongs to Phase 4, when adapters exist to be enabled.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.core.constants import FreshnessWindow


class PortalSummary(BaseModel):
    """One portal as the dashboard renders it.

    ``is_selectable`` is what the checkbox should honour: a portal is only
    usable once it is both enabled and backed by an implemented adapter.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    display_name: str
    website_url: str | None = None
    is_enabled: bool
    is_implemented: bool = Field(
        description="False until an adapter exists and passes contract tests."
    )
    supports_date_filter: bool
    supports_description_fetch: bool
    requires_auth: bool
    notes: str | None = None
    display_order: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_selectable(self) -> bool:
        return self.is_enabled and self.is_implemented


class PortalProbeRequest(BaseModel):
    """Ask one adapter to collect a handful of listings.

    A diagnostic, not a search: the limit is capped server-side and nothing is
    written to the database.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "queries": ["Financial Analyst", "FP&A Analyst"],
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
            description="Same-role title variations to search for.",
        ),
    ]
    limit: Annotated[int, Field(ge=1, le=10)] = 5
    window: FreshnessWindow = FreshnessWindow.LAST_7D
    location: str | None = None


class ProbeJob(BaseModel):
    """One collected listing, exactly as the adapter normalised it.

    This is the raw portal output, before any scoring, deduplication or
    ranking - none of which exist yet.
    """

    external_id: str | None = None
    source: str
    title: str
    company: str | None = None
    location: str | None = None
    description_chars: int = Field(
        default=0,
        description="Length of the collected description, if the portal gave one.",
    )
    source_url: str
    application_url: str | None = Field(
        default=None,
        description=(
            "The link a user opens to apply manually. May redirect onward to "
            "an employer site or an external application form."
        ),
    )
    posted_at: datetime | None = None
    employment_type: str | None = None


class PortalProbeResponse(BaseModel):
    """Result of exercising one adapter."""

    portal_slug: str
    ok: bool = Field(description="False when the portal refused or failed.")
    queries: list[str] = Field(default_factory=list)
    collected: int = 0
    duration_ms: int = 0
    jobs: list[ProbeJob] = Field(default_factory=list)

    error_code: str | None = Field(
        default=None,
        description="Portal error taxonomy code, e.g. portal_blocked.",
    )
    error_message: str | None = None

    health_available: bool | None = None
    health_detail: str | None = Field(
        default=None,
        description="Why the portal is unavailable, when it is.",
    )

    @classmethod
    def from_result(cls, result: Any) -> PortalProbeResponse:
        return cls(
            portal_slug=result.portal_slug,
            ok=result.ok,
            queries=list(result.queries),
            collected=len(result.jobs),
            duration_ms=result.duration_ms,
            jobs=[
                ProbeJob(
                    external_id=job.external_id,
                    source=job.source,
                    title=job.title,
                    company=job.company,
                    location=job.location,
                    description_chars=len(job.description or ""),
                    source_url=job.source_url,
                    application_url=job.application_url,
                    posted_at=job.posted_at,
                    employment_type=job.employment_type,
                )
                for job in result.jobs
            ],
            error_code=result.error_code,
            error_message=result.error_message,
            health_available=result.health_available,
            health_detail=result.health_detail,
        )
