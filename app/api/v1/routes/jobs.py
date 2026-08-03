"""Job listing and detail.

``resume_id`` is required, not optional: a job's score and its application
status both belong to a (resume, job) pair, so there is no correct way to list
jobs without knowing which resume is asking.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.core.constants import ApplicationStatus, SortOption
from app.schemas.common import Page, PaginationMeta, ResponseEnvelope
from app.schemas.job import JobDetail, JobListItem
from app.services import job_service, resume_service

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get(
    "",
    response_model=ResponseEnvelope[Page[JobListItem]],
    summary="List jobs for a resume",
    description=(
        "Results for one resume, with that resume's match score and manual "
        "application status attached.\n\n"
        "`resume_id` is required: scores and statuses are per resume and must "
        "never mix between resumes.\n\n"
        "Default sort is `best_overall`, which uses the stored ranking score. "
        "Expired listings are excluded unless `include_expired=true`; a job "
        "expiring never changes its application status."
    ),
    responses={404: {"description": "Resume not found."}},
)
async def list_jobs(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID, Query(description="Required.")],
    search_run_id: Annotated[uuid.UUID | None, Query()] = None,
    portal: Annotated[
        str | None, Query(description="Portal slug, e.g. `greenhouse`.")
    ] = None,
    status: Annotated[ApplicationStatus | None, Query()] = None,
    sort: Annotated[SortOption, Query()] = SortOption.BEST_OVERALL,
    include_expired: Annotated[bool, Query()] = False,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> ResponseEnvelope[Page[JobListItem]]:
    # Establishes both that the resume exists and that it belongs to the caller.
    resume = await resume_service.get_resume(
        session, resume_id=resume_id, user_id=user.id
    )

    items, total = await job_service.list_jobs(
        session,
        resume_id=resume.id,
        user_id=user.id,
        search_run_id=search_run_id,
        portal_slug=portal,
        status=status,
        sort=sort,
        include_expired=include_expired,
        page=page,
        page_size=page_size,
    )

    return ResponseEnvelope[Page[JobListItem]](
        data=Page[JobListItem](
            items=items,
            pagination=PaginationMeta.build(
                page=page, page_size=page_size, total=total
            ),
        )
    )


@router.get(
    "/{job_id}",
    response_model=ResponseEnvelope[JobDetail],
    summary="Get one job",
    description=(
        "One canonical job with its description and every portal source that "
        "carries it.\n\n"
        "When several portals list the same vacancy it is stored once, with "
        "all sources preserved and the most direct usable URL surfaced as "
        "`application_url`. The user opens that URL and applies manually."
    ),
    responses={404: {"description": "Job or resume not found."}},
)
async def get_job(
    job_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID, Query(description="Required.")],
) -> ResponseEnvelope[JobDetail]:
    resume = await resume_service.get_resume(
        session, resume_id=resume_id, user_id=user.id
    )

    job = await job_service.get_job(
        session, job_id=job_id, resume_id=resume.id, user_id=user.id
    )
    return ResponseEnvelope[JobDetail](data=job)
