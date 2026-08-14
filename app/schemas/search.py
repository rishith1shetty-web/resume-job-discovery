"""Search-run request and response schemas.

``POST /search-runs`` is the Find Matches endpoint: it records the run, then
executes it - collect, canonicalise, deduplicate, exclude, score, allocate,
order - and returns the qualified results synchronously.

Nothing in these schemas exposes credentials, cookies, tokens or a portal's
opaque adapter options. Raw portal payloads are stored for auditing but never
serialised here: a listing's usable fields are returned, not the response that
produced them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.constants import (
    MIN_MATCH_SCORE_FLOOR,
    ApplicationStatus,
    FreshnessWindow,
    PortalRunStatus,
    SearchRunStatus,
)

#: The only accepted values for `requested_limit` (CLAUDE.md section 5).
RequestedLimit = Literal[20, 50, 100]


class SearchRunCreate(BaseModel):
    """Start a search for one resume across the portals the user ticked.

    Every selected portal is searched. The limit is a target for *qualified*
    jobs, split into an approximately equal quota per portal.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "resume_id": "3f6c1f6e-7a5c-4a3e-9f1a-2b6d4c8e0a11",
                "selected_portal_ids": [
                    "9b1d0c2a-4e5f-4b7a-8c3d-1e2f3a4b5c6d",
                    "5c7e2b1a-3d4f-4a6b-9c8d-0e1f2a3b4c5d",
                ],
                "requested_limit": 20,
            }
        }
    )

    resume_id: uuid.UUID
    # At least one, at most the 8-portal ceiling from CLAUDE.md section 4.
    selected_portal_ids: Annotated[
        list[uuid.UUID],
        Field(
            min_length=1,
            max_length=8,
            description="Portals to search. Every one of them is searched.",
        ),
    ]
    requested_limit: RequestedLimit = Field(
        description="Target number of qualified jobs: 20, 50 or 100."
    )

    @field_validator("selected_portal_ids")
    @classmethod
    def _reject_duplicates(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("selected_portal_ids must not repeat a portal.")
        return value


class SearchRunPortalOut(BaseModel):
    """One portal's leg of a run.

    The counters narrow: ``raw_count`` is everything the portal returned,
    ``qualified_count`` what passed the threshold, ``selected_count`` what this
    portal contributed to the returned set.

    ``target_quota``, ``final_quota`` and ``shortfall`` are retained for
    compatibility and no longer gate anything: Phase 12 replaced per-portal
    quotas with one ranked candidate pool, so ``target_quota`` records the run's
    own limit and ``final_quota`` mirrors what the portal actually contributed.
    How deep each portal searched, and why it stopped, is reported on the run's
    ``summary`` under ``portals``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    portal_id: uuid.UUID
    portal_slug: str | None = None
    portal_display_name: str | None = None

    target_quota: int
    final_quota: int
    raw_count: int
    qualified_count: int
    selected_count: int
    shortfall: int = 0

    status: PortalRunStatus
    error: str | None = None
    deepest_window: FreshnessWindow | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class SearchRunOut(BaseModel):
    """A search run and its per-portal breakdown."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    resume_id: uuid.UUID
    profile_id: uuid.UUID | None = None

    requested_limit: int
    min_score: int = Field(
        description=f"Locked at {MIN_MATCH_SCORE_FLOOR} or higher; never lowered."
    )
    status: SearchRunStatus

    searched_domain: str | None = None
    searched_target_role: str | None = None

    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    summary: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "How the run reached its result: candidate pool size, collected "
            "and filtered counts, and each portal's search depth and "
            "exhaustion reason, once the run executes."
        ),
    )

    created_at: datetime
    portals: list[SearchRunPortalOut] = Field(default_factory=list)


class SearchRunProgressOut(BaseModel):
    """Progress for the dashboard's per-portal progress display.

    Reported per portal so a failure is visibly scoped to one portal rather
    than to the run (CLAUDE.md section 10).
    """

    id: uuid.UUID
    status: SearchRunStatus
    is_terminal: bool
    portal_count: int
    portals_completed: int
    portals_failed: int
    total_selected: int
    requested_limit: int
    portals: list[SearchRunPortalOut] = Field(default_factory=list)


class SearchQueryOut(BaseModel):
    """One query issued against one portal, for auditing what was searched."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    search_run_portal_id: uuid.UUID
    query_text: str
    window: FreshnessWindow
    page: int
    result_count: int
    executed_at: datetime | None = None
    duration_ms: int | None = None


class ExecutedQueryOut(BaseModel):
    """A query as executed during a run, without needing its stored row."""

    query_text: str
    window: FreshnessWindow
    result_count: int
    portal: str


class SearchResultOut(BaseModel):
    """One qualified job in a run's result set.

    Every field the dashboard shows (CLAUDE.md section 10). There is no Easy
    Apply flag, apply priority, form progress or submit result: the user opens
    ``preferred_application_url`` and applies manually.
    """

    job_id: uuid.UUID
    title: str
    company: str | None = None
    location: str | None = None
    work_mode: str | None = None
    posted_at: datetime | None = None

    final_score: float = Field(description="Resume match, 0-100. Always >= 70 here.")
    ranking_score: float = Field(
        description="Match, freshness and source quality blended for sorting."
    )
    freshness_score: float
    qualified: bool

    role_verdict: str
    match_explanation: str = Field(
        description="Why this posting is the candidate's role."
    )
    positive_evidence: list[str] = Field(default_factory=list)

    portal_slugs: list[str] = Field(
        default_factory=list,
        description="Every portal carrying this job, not only the first.",
    )
    discovered_via: str = Field(
        description="The portal whose quota this job was counted against."
    )

    preferred_application_url: str | None = None
    preferred_url_kind: str | None = None

    application_status: ApplicationStatus = Field(
        description="This resume's status for this job. Never auto-changed."
    )
    is_new: bool = Field(
        description="First discovered in this run, rather than rediscovered."
    )


class CareerPageOut(BaseModel):
    """One company and the official career page it publishes.

    Deliberately not a ``SearchResultOut``. There is no ``job_id``, no
    ``final_score``, no ``posted_at`` and no ``application_status``, because a
    career page is a company's recruiting site rather than a vacancy - it has
    none of those things, and inventing them would be claiming evidence that
    does not exist.

    ``relevance`` is a company-level judgement built from the openings actually
    observed. It is not the 0-100 resume match a scored job carries and is
    never compared against the 70% threshold.
    """

    company_name: str
    relevant_role: str = Field(
        description="The role titles seen at this company, as the employer writes them."
    )
    location: str
    official_career_url: str = Field(
        description=(
            "The employer's own careers site, or their own space on an "
            "applicant-tracking system. Never an aggregator or job board."
        )
    )
    url_kind: str = Field(description="Why the URL was accepted as official.")
    opening_count: int = Field(
        description="Current US openings seen matching the resume's target role."
    )
    relevance: float = Field(
        description="Company-level relevance, 0-100. Not a resume match score."
    )
    reason: str = Field(description="Why this company and page are being shown.")
    sources: list[str] = Field(
        default_factory=list, description="Portals that supplied the evidence."
    )
    matched_titles: list[str] = Field(default_factory=list)


class SearchRunResultsOut(BaseModel):
    """A run's outcome: what was searched, what came back, and what failed."""

    search_run_id: uuid.UUID
    resume_id: uuid.UUID
    status: SearchRunStatus
    requested_limit: int
    returned_count: int
    min_score: int = Field(
        description=f"Locked at {MIN_MATCH_SCORE_FLOOR}. Never lowered to fill a count."
    )

    target_role: str | None = None
    domain: str | None = None

    is_short: bool = Field(
        description="True when fewer jobs qualified than were requested."
    )

    results: list[SearchResultOut] = Field(default_factory=list)
    career_pages: list[CareerPageOut] = Field(
        default_factory=list,
        description=(
            "Companies with current US openings and their official career "
            "pages, when a career source was selected. A supplementary "
            "category: these took no part in filling `results`, and "
            "`requested_limit` does not apply to them."
        ),
    )
    portals: list[SearchRunPortalOut] = Field(default_factory=list)
    queries: list[ExecutedQueryOut] = Field(default_factory=list)
    warnings: list[str] = Field(
        default_factory=list,
        description="Portal failures and shortfalls, stated rather than hidden.",
    )
    summary: dict[str, Any] = Field(default_factory=dict)
