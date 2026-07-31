"""Public client configuration.

Served to the React frontend so the dashboard renders its controls - limit
buttons, sort menu, status dropdown, freshness labels - from server data
instead of maintaining a duplicate copy of these constants.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EnumOption(BaseModel):
    """A selectable value plus its display label."""

    value: str
    label: str


class SearchConfig(BaseModel):
    result_limit_options: list[int] = Field(
        description="Allowed values for requested_limit on a search run."
    )
    default_result_limit: int
    min_match_score: int = Field(
        description=(
            "Locked qualification threshold. The backend never lowers this to "
            "fill a quota; the frontend must render it as read-only."
        )
    )
    min_match_score_locked: bool = True
    max_enabled_portals: int
    freshness_windows: list[EnumOption] = Field(
        description="Progressive search windows, ordered newest-first."
    )
    sort_options: list[EnumOption]
    default_sort: str


class ResumeConfig(BaseModel):
    supported_extensions: list[str]
    supported_content_types: list[str]
    max_upload_mb: int


class ApplicationConfig(BaseModel):
    statuses: list[EnumOption] = Field(
        description=(
            "Manual application statuses. Contains no auto-apply concepts. "
            "'Expired' is a job-level property, not an application status."
        )
    )
    default_status: str


class ClientConfigResponse(BaseModel):
    """Returned by ``GET /api/v1/config``."""

    app_name: str
    version: str
    environment: str
    api_version: str = "v1"
    api_prefix: str
    # Phase 1 ships no authentication. Documented so the frontend can build
    # its fetch wrapper once. See docs/FRONTEND_INTEGRATION.md.
    auth_required: bool = False
    auth_scheme: str | None = None
    search: SearchConfig
    resume: ResumeConfig
    application: ApplicationConfig
