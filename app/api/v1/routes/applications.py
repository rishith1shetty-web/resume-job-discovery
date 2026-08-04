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
from app.schemas.application import (
    ApplicationDetailOut,
    ApplicationHistoryItem,
    ApplicationOut,
    ApplicationStatusHistoryOut,
    ApplicationUpdateRequest,
)
from app.schemas.common import Page, PaginationMeta, ResponseEnvelope
from app.services import application_service
from app.services.application_service import ApplicationView

router = APIRouter(tags=["Applications"])


def _application_out(view: ApplicationView) -> ApplicationOut:
    application, job = view.application, view.job
    return ApplicationOut(
        id=application.id,
        resume_id=application.resume_id,
        job_id=application.job_id,
        status=application.status,
        applied_at=application.applied_at,
        notes=application.notes,
        has_notes=view.has_notes,
        created_at=application.created_at,
        updated_at=application.updated_at,
        status_changed_at=view.effective_status_changed_at,
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
    _, changed = await application_service.set_status(
        session,
        user_id=user.id,
        resume_id=resume_id,
        job_id=job_id,
        status=payload.status,
        notes=payload.notes,
    )
    await session.commit()

    # Re-read rather than refresh: this also picks up the job fields and the
    # derived `status_changed_at`, so one schema does not mean two different
    # payloads depending on which endpoint produced it.
    view = await application_service.load_application_view(
        session, user_id=user.id, resume_id=resume_id, job_id=job_id
    )
    assert view is not None  # set_status has just created or updated it

    return ResponseEnvelope[ApplicationOut](
        data=_application_out(view),
        message=None if changed else "Status was already set; nothing changed.",
    )


@router.get(
    "/applications/{job_id}",
    response_model=ResponseEnvelope[ApplicationDetailOut],
    summary="Get one job's tracking state",
    description=(
        "The status, note, timestamps and full transition history for one "
        "canonical job under one resume.\n\n"
        "A job the user has never tracked is not an error: `application` is "
        "null and `status` is `not_applied`. The key is the canonical job, so "
        "a vacancy found through several portals has exactly one state here."
    ),
    responses={404: {"description": "Job or resume not found."}},
)
async def get_application(
    job_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID, Query(description="Required.")],
) -> ResponseEnvelope[ApplicationDetailOut]:
    # Validated even when nothing is tracked yet, so a bad id is reported as
    # such instead of silently answering "not applied".
    await application_service.require_resume_and_job(
        session, user_id=user.id, resume_id=resume_id, job_id=job_id
    )

    view = await application_service.load_application_view(
        session, user_id=user.id, resume_id=resume_id, job_id=job_id
    )

    if view is None:
        return ResponseEnvelope[ApplicationDetailOut](
            data=ApplicationDetailOut(
                application=None,
                status=ApplicationStatus.NOT_APPLIED,
                history=[],
            ),
            message="Nothing recorded for this job under this resume yet.",
        )

    rows, _ = await application_service.list_status_history(
        session,
        user_id=user.id,
        resume_id=resume_id,
        job_id=job_id,
        page=1,
        page_size=200,
    )

    return ResponseEnvelope[ApplicationDetailOut](
        data=ApplicationDetailOut(
            application=_application_out(view),
            status=view.application.status,
            history=[
                ApplicationStatusHistoryOut.model_validate(history)
                for history, _, _ in rows
            ],
        )
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
            items=[_application_out(view) for view in applications],
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
        "correction adds a new transition rather than editing an old one.\n\n"
        "Each row names the resume and the *canonical* job. Raw portal "
        "captures are never exposed here - a listing records what a portal "
        "returned, not something the user applied to.\n\n"
        "Filter by `job_id` for one job's own timeline, or by `resume_id` to "
        "keep two resumes' histories apart."
    ),
)
async def list_application_history(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID | None, Query()] = None,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ResponseEnvelope[Page[ApplicationHistoryItem]]:
    rows, total = await application_service.list_status_history(
        session,
        user_id=user.id,
        resume_id=resume_id,
        job_id=job_id,
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
                    resume_id=application.resume_id,
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
