"""Read-only match preview.

Scores stored canonical jobs against one resume and explains every verdict,
without writing anything. Phase 7 owns the search run that will persist these
scores; this endpoint exists so the rules can be inspected first.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import DbSession
from app.schemas.common import ResponseEnvelope
from app.schemas.matching import MatchPreviewRequest, MatchPreviewResponse
from app.services import match_service

router = APIRouter(prefix="/resumes", tags=["Matching"])


@router.post(
    "/{resume_id}/match-preview",
    response_model=ResponseEnvelope[MatchPreviewResponse],
    summary="Score stored jobs against one resume",
    description=(
        "Scores canonical jobs for one resume and returns the reasoning behind "
        "each verdict.\n\n"
        "**Writes nothing.** Only SELECT statements are issued: no match, job "
        "or application row is created or modified.\n\n"
        "Scoring is deterministic and uses no language model - the same resume "
        "and job always produce the same score and the same explanation. The "
        "role gate runs before skill scoring, so a posting for a different "
        "profession is rejected however many skills it shares.\n\n"
        "A job qualifies only at 70% or above with no hard rule failed. That "
        "threshold is locked and is never lowered to fill a result count. "
        "`is_presentable` additionally excludes jobs already applied to with "
        "this resume; those stay in application history and are not offered "
        "again.\n\n"
        "This is not the job search. Collection, quotas, freshness windows and "
        "ranking arrive in Phase 7."
    ),
    responses={
        404: {"description": "Resume has no analysed profile."},
        422: {"description": "Resume has no detected target role."},
    },
)
async def preview_matches(
    resume_id: uuid.UUID,
    payload: MatchPreviewRequest,
    session: DbSession,
) -> ResponseEnvelope[MatchPreviewResponse]:
    profile = await match_service.load_match_profile(session, resume_id=resume_id)

    jobs = await match_service.load_jobs(
        session, job_ids=payload.job_ids, limit=payload.limit
    )
    summary = await match_service.score_jobs_for_resume(
        session, resume_id=resume_id, jobs=jobs, persist=False
    )

    message = None
    if not jobs:
        message = (
            "No stored jobs to score yet. Jobs are created by portal "
            "processing."
        )
    elif summary.presentable_count == 0:
        message = "No job in this sample reached the 70% threshold."

    return ResponseEnvelope[MatchPreviewResponse](
        data=MatchPreviewResponse.from_summary(
            summary=summary,
            profile=profile,
            jobs_by_id={job.id: job for job in jobs},
            include_rejected=payload.include_rejected,
        ),
        message=message,
    )
