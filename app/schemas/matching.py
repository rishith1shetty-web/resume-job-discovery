"""Schemas for the read-only match preview.

The preview exists so a score can be inspected before anything is stored:
what the role gate decided, which skills were found and missed, and why a job
did or did not qualify.

Nothing here submits an application. A qualifying job is a link the user opens
and applies through by hand (CLAUDE.md section 1).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import MIN_MATCH_SCORE_FLOOR


class MatchPreviewRequest(BaseModel):
    """Score a set of stored canonical jobs against one resume."""

    model_config = ConfigDict(
        json_schema_extra={"example": {"limit": 10, "include_rejected": True}}
    )

    job_ids: Annotated[
        list[uuid.UUID] | None,
        Field(
            default=None,
            max_length=200,
            description=(
                "Canonical job ids to score. Omit to score the most recently "
                "posted stored jobs."
            ),
        ),
    ] = None
    limit: Annotated[int, Field(ge=1, le=50)] = 10
    include_rejected: bool = Field(
        default=True,
        description=(
            "Return jobs that did not qualify, with their rejection reason. "
            "Rejected jobs are never presented as results; this only controls "
            "whether the preview shows them."
        ),
    )


class RoleAssessmentOut(BaseModel):
    """What the role gate decided, and why."""

    verdict: str = Field(
        description="exact, variation, strong_variant, weak_variant or incompatible."
    )
    reason: str
    detected_role_key: str | None = None


class SkillAssessmentOut(BaseModel):
    """Skill coverage, split by how firmly the posting asked."""

    matched_required: list[str] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    matched_preferred: list[str] = Field(default_factory=list)
    missing_mandatory: list[str] = Field(
        default_factory=list,
        description="Must-have skills the resume does not evidence. These reject.",
    )
    required_coverage: float = Field(ge=0.0, le=1.0)
    preferred_coverage: float = Field(ge=0.0, le=1.0)


class ComponentScoreOut(BaseModel):
    """One weighted component of the total."""

    score: float
    weight: float


class MatchPreviewJob(BaseModel):
    """One job scored against the resume."""

    job_id: uuid.UUID
    title: str
    company: str | None = None
    location: str | None = None
    posted_at: Any | None = None
    preferred_application_url: str | None = Field(
        default=None,
        description="The link to open to apply by hand. Never opened by the system.",
    )

    final_score: float = Field(ge=0.0, le=100.0)
    qualified: bool = Field(
        description=f"True only at {MIN_MATCH_SCORE_FLOOR}% or above, with no hard rule failed."
    )
    is_presentable: bool = Field(
        description=(
            "Qualified and still eligible: a job already applied to with this "
            "resume is excluded from future results."
        )
    )
    is_eligible: bool = True
    eligibility_reason: str = ""

    rejection_code: str | None = None
    rejection_reason: str | None = None

    components: dict[str, ComponentScoreOut] = Field(default_factory=dict)
    role: RoleAssessmentOut
    skills: SkillAssessmentOut
    experience_summary: str = ""
    domain_summary: str = ""
    education_summary: str = ""
    positive_evidence: list[str] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)

    scoring_version: str


class MatchPreviewResponse(BaseModel):
    """What matching would decide for this resume, with nothing stored."""

    resume_id: uuid.UUID
    target_role: str | None = None
    domain: str | None = None
    threshold: float = float(MIN_MATCH_SCORE_FLOOR)
    scoring_version: str

    evaluated: int = 0
    qualified: int = 0
    presentable: int = Field(
        default=0, description="Qualified and not already applied to."
    )
    excluded_as_applied: int = 0

    jobs: list[MatchPreviewJob] = Field(default_factory=list)

    @classmethod
    def from_summary(
        cls,
        *,
        summary: Any,
        profile: Any,
        jobs_by_id: dict[uuid.UUID, Any],
        include_rejected: bool,
    ) -> MatchPreviewResponse:
        items: list[MatchPreviewJob] = []

        for scored in summary.scored:
            if not include_rejected and not scored.qualified:
                continue

            job = jobs_by_id.get(scored.job_id)
            result = scored.result
            details = result.to_details()

            items.append(
                MatchPreviewJob(
                    job_id=scored.job_id,
                    title=scored.title,
                    company=scored.company,
                    location=getattr(job, "location", None),
                    posted_at=getattr(job, "posted_at", None),
                    preferred_application_url=getattr(
                        job, "preferred_application_url", None
                    ),
                    final_score=scored.final_score,
                    qualified=scored.qualified,
                    is_presentable=scored.is_presentable,
                    is_eligible=scored.eligibility.is_eligible,
                    eligibility_reason=scored.eligibility.reason,
                    rejection_code=scored.rejection_code,
                    rejection_reason=scored.rejection_reason,
                    components={
                        name: ComponentScoreOut(**payload)
                        for name, payload in details["components"].items()
                    },
                    role=RoleAssessmentOut(**details["role"]),
                    skills=SkillAssessmentOut(**details["skills"]),
                    experience_summary=details["experience"]["summary"],
                    domain_summary=details["domain"],
                    education_summary=details["education"],
                    positive_evidence=details["positive_evidence"],
                    penalties=details["penalties"],
                    scoring_version=result.scoring_version,
                )
            )

        return cls(
            resume_id=summary.resume_id,
            target_role=profile.target_role_display,
            domain=profile.domain_key,
            scoring_version=summary.scoring_version,
            evaluated=len(summary.scored),
            qualified=summary.qualified_count,
            presentable=summary.presentable_count,
            excluded_as_applied=summary.excluded_as_applied,
            jobs=items,
        )
