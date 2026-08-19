"""Career page interaction endpoints.

The same lifecycle jobs have at ``/applications``, for the company career pages
a search surfaces. The user opens the employer's site and applies there
themselves; these endpoints only record what they say they did.

Naming follows the job side: ``PATCH`` sets the decision, ``GET`` by key reads
one, ``GET`` on the collection lists them for history.

A career page is identified by its canonicalised URL rather than by the company
name, so the client sends the page and the server derives the key. That keeps
one decision per page even when two runs surface it with different tracking
parameters.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.core.constants import ApplicationStatus
from app.schemas.career_page import (
    CareerPageDecisionRequest,
    CareerPageInteractionOut,
    CareerPageStateOut,
)
from app.schemas.common import Page, PaginationMeta, ResponseEnvelope
from app.services import career_page_service

router = APIRouter(tags=["Career pages"])


@router.patch(
    "/career-pages/decision",
    response_model=ResponseEnvelope[CareerPageInteractionOut],
    summary="Mark a career page Applied or Skipped",
    description=(
        "Records the decision the user made about one company career page "
        "under one resume.\n\n"
        "Only `applied` and `skipped` are accepted: a career page is a place "
        "to go and look, not a vacancy with an interview or an offer. Both "
        "decisions remove the page from future Find Matches runs **for that "
        "resume only** - another resume still sees it.\n\n"
        "Sending the decision the page already has changes nothing and is not "
        "an error, so a double click is safe.\n\n"
        "Applying to a career page does **not** apply to the jobs listed on "
        "it, and skipping one does not skip them. No job application record is "
        "created or modified by this endpoint."
    ),
    responses={
        404: {"description": "Resume not found."},
        422: {"description": "Unusable status, or a page that cannot be identified."},
    },
)
async def set_career_page_decision(
    payload: CareerPageDecisionRequest,
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID, Query(description="Required.")],
) -> ResponseEnvelope[CareerPageInteractionOut]:
    page = career_page_service.identify(
        official_career_url=payload.official_career_url,
        company_name=payload.company_name,
        relevant_role=payload.relevant_role,
        location=payload.location,
    )

    interaction, changed = await career_page_service.set_status(
        session,
        user_id=user.id,
        resume_id=resume_id,
        page=page,
        status=payload.status,
        notes=payload.notes,
    )
    await session.commit()
    await session.refresh(interaction)

    return ResponseEnvelope[CareerPageInteractionOut](
        data=CareerPageInteractionOut.model_validate(interaction),
        message=None if changed else "Already recorded; nothing changed.",
    )


@router.delete(
    "/career-pages/decision",
    response_model=ResponseEnvelope[CareerPageStateOut],
    summary="Undo a career page decision",
    description=(
        "Removes the recorded decision, making the page eligible for this "
        "resume's future searches again. Undoing a decision that was never "
        "made is not an error."
    ),
    responses={404: {"description": "Resume not found."}},
)
async def clear_career_page_decision(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID, Query(description="Required.")],
    official_career_url: Annotated[str, Query(description="Required.")],
    company_name: Annotated[str | None, Query()] = None,
) -> ResponseEnvelope[CareerPageStateOut]:
    page = career_page_service.identify(
        official_career_url=official_career_url, company_name=company_name
    )

    removed = await career_page_service.clear_status(
        session, user_id=user.id, resume_id=resume_id, career_page_key=page.key
    )
    await session.commit()

    return ResponseEnvelope[CareerPageStateOut](
        data=CareerPageStateOut(
            interaction=None, status=None, career_page_key=page.key
        ),
        message=None if removed else "No decision was recorded for this page.",
    )


@router.get(
    "/career-pages/state",
    response_model=ResponseEnvelope[CareerPageStateOut],
    summary="Get one career page's decision",
    description=(
        "The decision recorded for one career page under one resume.\n\n"
        "A page the user has never acted on is not an error: `interaction` is "
        "null and `status` is null."
    ),
    responses={422: {"description": "The page cannot be identified."}},
)
async def get_career_page_state(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID, Query(description="Required.")],
    official_career_url: Annotated[str, Query(description="Required.")],
    company_name: Annotated[str | None, Query()] = None,
) -> ResponseEnvelope[CareerPageStateOut]:
    page = career_page_service.identify(
        official_career_url=official_career_url, company_name=company_name
    )

    interaction = await career_page_service.get_interaction(
        session, user_id=user.id, resume_id=resume_id, career_page_key=page.key
    )

    return ResponseEnvelope[CareerPageStateOut](
        data=CareerPageStateOut(
            interaction=(
                CareerPageInteractionOut.model_validate(interaction)
                if interaction is not None
                else None
            ),
            status=interaction.status if interaction is not None else None,
            career_page_key=page.key,
        )
    )


@router.get(
    "/career-pages",
    response_model=ResponseEnvelope[Page[CareerPageInteractionOut]],
    summary="List career page decisions",
    description=(
        "Career pages the user has acted on, most recently changed first.\n\n"
        "Filter by `resume_id` to see one resume's decisions and by `status` "
        "to separate Applied from Skipped. Decisions are stored per resume, so "
        "the same employer acted on under two resumes has two independent "
        "records.\n\n"
        "Every item carries `kind: \"career_page\"`, so an Application History "
        "view can merge these with job applications and still show which is "
        "which."
    ),
)
async def list_career_page_decisions(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID | None, Query()] = None,
    status: Annotated[ApplicationStatus | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ResponseEnvelope[Page[CareerPageInteractionOut]]:
    interactions, total = await career_page_service.list_interactions(
        session,
        user_id=user.id,
        resume_id=resume_id,
        status=status,
        page=page,
        page_size=page_size,
    )

    return ResponseEnvelope[Page[CareerPageInteractionOut]](
        data=Page[CareerPageInteractionOut](
            items=[
                CareerPageInteractionOut.model_validate(item) for item in interactions
            ],
            pagination=PaginationMeta.build(
                page=page, page_size=page_size, total=total
            ),
        )
    )
