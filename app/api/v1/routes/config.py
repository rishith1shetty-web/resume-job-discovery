"""Client configuration endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.constants import (
    FRESHNESS_ESCALATION,
    RESULT_LIMIT_OPTIONS,
    SUPPORTED_RESUME_CONTENT_TYPES,
    SUPPORTED_RESUME_EXTENSIONS,
    ApplicationStatus,
    SortOption,
)
from app.schemas.common import ResponseEnvelope
from app.schemas.config import (
    ApplicationConfig,
    ClientConfigResponse,
    EnumOption,
    ResumeConfig,
    SearchConfig,
)

router = APIRouter(tags=["System"])


def _humanise(value: str) -> str:
    return value.replace("_", " ").title()


_FRESHNESS_LABELS = {
    "last_24h": "Last 24 hours",
    "last_3d": "Last 3 days",
    "last_7d": "Last 7 days",
    "last_14d": "Last 14 days",
    "last_30d": "Last 30 days",
    "older_active": "Older, still active",
}


@router.get(
    "/config",
    response_model=ResponseEnvelope[ClientConfigResponse],
    summary="Client configuration",
    description=(
        "Constants the dashboard renders from - result limits, the locked "
        "match threshold, freshness windows, sort options and application "
        "statuses. The frontend should read these rather than hardcode them."
    ),
)
async def client_config() -> ResponseEnvelope[ClientConfigResponse]:
    settings = get_settings()

    payload = ClientConfigResponse(
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
        api_prefix=settings.api_v1_prefix,
        auth_required=False,
        auth_scheme=None,
        search=SearchConfig(
            result_limit_options=list(RESULT_LIMIT_OPTIONS),
            default_result_limit=RESULT_LIMIT_OPTIONS[0],
            min_match_score=settings.min_match_score,
            min_match_score_locked=True,
            max_enabled_portals=settings.max_enabled_portals,
            freshness_windows=[
                EnumOption(
                    value=window.value,
                    label=_FRESHNESS_LABELS.get(window.value, _humanise(window.value)),
                )
                for window in FRESHNESS_ESCALATION
            ],
            sort_options=[
                EnumOption(value=option.value, label=_humanise(option.value))
                for option in SortOption
            ],
            default_sort=SortOption.BEST_OVERALL.value,
        ),
        resume=ResumeConfig(
            supported_extensions=list(SUPPORTED_RESUME_EXTENSIONS),
            supported_content_types=list(SUPPORTED_RESUME_CONTENT_TYPES),
            max_upload_mb=settings.max_upload_mb,
        ),
        application=ApplicationConfig(
            statuses=[
                EnumOption(value=item.value, label=_humanise(item.value))
                for item in ApplicationStatus
            ],
            default_status=ApplicationStatus.NOT_APPLIED.value,
        ),
    )

    return ResponseEnvelope[ClientConfigResponse](data=payload)
