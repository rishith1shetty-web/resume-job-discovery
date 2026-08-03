"""Search-run request and response schemas.

``POST /search-runs`` records the run and its per-portal quotas. It does not
collect anything: portal adapters arrive in Phase 4 and orchestration in Phase
7, so a newly created run stays PENDING until then. The contract is fixed now
so the frontend can be built against it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.constants import (
    MIN_MATCH_SCORE_FLOOR,
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
    ``qualified_count`` what passed the threshold, ``selected_count`` what the
    quota took. ``shortfall`` is what could not be filled with qualified jobs -
    a weak job is never used to close it.
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
        description="Quota shortages and redistributions, once the run executes.",
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
