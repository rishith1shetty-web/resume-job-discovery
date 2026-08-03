"""Manual application tracking endpoints.

The user opens the job URL, applies themselves, and records the outcome here.
There is no endpoint that submits an application, and there never will be.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.core.constants import ApplicationStatus
from app.models.application import Application
from app.models.job import Job
from app.schemas.application import (
    ApplicationHistoryItem,
    ApplicationOut,
    ApplicationUpdateRequest,
)
from app.schemas.common import Page, PaginationMeta, ResponseEnvelope
from app.services import application_service

router = APIRouter(tags=["Applications"])


def _application_out(
    application: Application, job: Job | None = None
) -> ApplicationOut:
    return ApplicationOut(
        id=application.id,
        resume_id=application.resume_id,
        job_id=application.job_id,
        status=application.status,
        applied_at=application.applied_at,
        notes=application.notes,
        created_at=application.created_at,
        updated_at=application.updated_at,
        job_title=getattr(job, "title", None),
        company=getattr(job, "company", None),
        application_url=getattr(job, "preferred_application_url", None),
    )


@router.patch(
    "/applications/{job_id}",
    response_model=ResponseEnvelope[ApplicationOut],
    summary="Set the manual application status",
    description=(
        "Records the status the user chose for one job under one resume.\n\n"
        "Creates the application record on first use. Setting `applied` "
        "stamps `applied_at`; moving on to `interview`, `rejected` or `offer` "
        "keeps that original timestamp. Sending the status the application "
        "already has changes nothing and writes no history entry.\n\n"
        "There is no `expired` status: expiry belongs to the job, and a "
        "listing going stale never overwrites what the user recorded."
    ),
    responses={
        404: {"description": "Job or resume not found."},
        422: {"description": "Unknown status value."},
    },
)
async def update_application(
    job_id: uuid.UUID,
    payload: ApplicationUpdateRequest,
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID, Query(description="Required.")],
) -> ResponseEnvelope[ApplicationOut]:
    application, changed = await application_service.set_status(
        session,
        user_id=user.id,
        resume_id=resume_id,
        job_id=job_id,
        status=payload.status,
        notes=payload.notes,
    )
    await session.commit()
    # `updated_at` carries a server-side onupdate, so it is expired by the
    # UPDATE. Reading it without this refresh would attempt lazy IO from an
    # async context and fail.
    await session.refresh(application)

    # Loaded so this response carries the same job fields the list endpoint
    # returns; one schema should not mean two different payloads.
    job = await session.get(Job, application.job_id)

    return ResponseEnvelope[ApplicationOut](
        data=_application_out(application, job),
        message=None if changed else "Status was already set; nothing changed.",
    )


@router.get(
    "/applications",
    response_model=ResponseEnvelope[Page[ApplicationOut]],
    summary="List applications",
    description=(
        "Applications for the current user, most recently updated first.\n\n"
        "Filter by `resume_id` to see one resume's applications. Application "
        "state is stored per resume, so the same job tracked under two "
        "resumes has two independent statuses."
    ),
)
async def list_applications(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[ApplicationStatus | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> ResponseEnvelope[Page[ApplicationOut]]:
    applications, total = await application_service.list_applications(
        session,
        user_id=user.id,
        resume_id=resume_id,
        status=status,
        page=page,
        page_size=page_size,
    )

    return ResponseEnvelope[Page[ApplicationOut]](
        data=Page[ApplicationOut](
            items=[
                _application_out(application, application.job)
                for application in applications
            ],
            pagination=PaginationMeta.build(
                page=page, page_size=page_size, total=total
            ),
        )
    )


@router.get(
    "/application-history",
    response_model=ResponseEnvelope[Page[ApplicationHistoryItem]],
    summary="List status-change history",
    description=(
        "Every status transition, newest first. Records are immutable: a "
        "correction adds a new transition rather than editing an old one."
    ),
)
async def list_application_history(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ResponseEnvelope[Page[ApplicationHistoryItem]]:
    rows, total = await application_service.list_status_history(
        session,
        user_id=user.id,
        resume_id=resume_id,
        page=page,
        page_size=page_size,
    )

    return ResponseEnvelope[Page[ApplicationHistoryItem]](
        data=Page[ApplicationHistoryItem](
            items=[
                ApplicationHistoryItem(
                    id=history.id,
                    application_id=history.application_id,
                    old_status=history.old_status,
                    new_status=history.new_status,
                    changed_at=history.changed_at,
                    changed_by=history.changed_by,
                    note=history.note,
                    job_id=application.job_id,
                    job_title=job.title,
                    company=job.company,
                )
                for history, application, job in rows
            ],
            pagination=PaginationMeta.build(
                page=page, page_size=page_size, total=total
            ),
        )
    )
