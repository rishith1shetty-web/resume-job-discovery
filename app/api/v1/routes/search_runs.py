"""Search-run endpoints - the Find Matches workflow.

``POST /search-runs`` records a run and executes it synchronously: it collects
from every selected portal, canonicalises and deduplicates, drops jobs this
resume has already applied with, scores what remains, keeps only the qualified,
allocates a fair share per portal and returns them in a deterministic order.

The 70% threshold is locked and is not accepted as input. A run that finds
fewer qualified jobs than requested returns the smaller set and says so.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import AppSettings, CurrentUser, DbSession
from app.core.constants import ApplicationStatus, PortalRunStatus
from app.models.search import SearchRun, SearchRunPortal
from app.schemas.common import Page, PaginationMeta, ResponseEnvelope
from app.schemas.search import (
    CareerPageOut,
    ExecutedQueryOut,
    SearchResultOut,
    SearchRunCreate,
    SearchRunOut,
    SearchRunPortalOut,
    SearchRunProgressOut,
    SearchRunResultsOut,
)
from app.services import resume_service, search_run_service
from app.services.search_orchestrator import SearchOutcome, execute_search_run

router = APIRouter(prefix="/search-runs", tags=["Search"])


def _portal_out(leg: SearchRunPortal) -> SearchRunPortalOut:
    portal = leg.portal
    return SearchRunPortalOut(
        id=leg.id,
        portal_id=leg.portal_id,
        portal_slug=portal.slug if portal else None,
        portal_display_name=portal.display_name if portal else None,
        target_quota=leg.target_quota,
        final_quota=leg.final_quota,
        raw_count=leg.raw_count,
        qualified_count=leg.qualified_count,
        selected_count=leg.selected_count,
        shortfall=leg.shortfall,
        status=leg.status,
        error=leg.error,
        deepest_window=leg.deepest_window,
        started_at=leg.started_at,
        completed_at=leg.completed_at,
    )


def _run_out(run: SearchRun) -> SearchRunOut:
    return SearchRunOut(
        id=run.id,
        resume_id=run.resume_id,
        profile_id=run.profile_id,
        requested_limit=run.requested_limit,
        min_score=run.min_score,
        status=run.status,
        searched_domain=run.searched_domain,
        searched_target_role=run.searched_target_role,
        started_at=run.started_at,
        completed_at=run.completed_at,
        error_message=run.error_message,
        summary=dict(run.summary or {}),
        created_at=run.created_at,
        portals=[_portal_out(leg) for leg in run.portals],
    )


def _result_out(item) -> SearchResultOut:
    return SearchResultOut(
        job_id=item.job_id,
        title=item.title,
        company=item.company,
        location=item.location,
        work_mode=item.work_mode,
        posted_at=item.posted_at,
        final_score=item.final_score,
        ranking_score=item.ranking_score,
        freshness_score=item.freshness_score,
        qualified=item.qualified,
        role_verdict=item.role_verdict,
        match_explanation=item.match_explanation,
        positive_evidence=list(item.positive_evidence),
        portal_slugs=list(item.portal_slugs),
        discovered_via=item.discovered_via,
        preferred_application_url=item.preferred_application_url,
        preferred_url_kind=item.preferred_url_kind,
        application_status=item.application_status,
        is_new=item.is_new,
    )


def _stored_result_out(item, statuses: dict) -> SearchResultOut:
    """Rebuild one result row from what the run stored.

    Scores and the explanation come from that run's own ``job_matches`` row, so
    the numbers shown are the ones the run actually produced. Only the
    application status is read live - it is the one thing the user changes
    afterwards.
    """
    job, match = item.job, item.match
    details = dict(match.score_details or {})

    return SearchResultOut(
        job_id=job.id,
        title=job.title,
        company=job.company,
        location=job.location,
        work_mode=job.work_mode.value if job.work_mode else None,
        posted_at=job.posted_at,
        final_score=match.final_score,
        ranking_score=match.ranking_score,
        freshness_score=match.freshness_score,
        qualified=match.qualified,
        role_verdict=str((details.get("role") or {}).get("verdict", "")),
        match_explanation=str((details.get("role") or {}).get("reason", "")),
        positive_evidence=list(details.get("positive_evidence", ()) or ()),
        portal_slugs=list(item.portal_slugs),
        discovered_via="",
        preferred_application_url=job.preferred_application_url,
        preferred_url_kind=(
            job.preferred_url_kind.value if job.preferred_url_kind else None
        ),
        application_status=statuses.get(job.id, ApplicationStatus.NOT_APPLIED),
        is_new=False,
    )


def _career_page_out(page) -> CareerPageOut:
    """Serialise one company career page.

    No application status and no job id: nothing here is a vacancy, and a
    career page never creates or carries an application record.
    """
    return CareerPageOut(
        company_name=page.company_name,
        relevant_role=page.relevant_role,
        location=page.location,
        official_career_url=page.official_career_url,
        url_kind=page.url_kind.value,
        opening_count=page.opening_count,
        relevance=page.relevance,
        reason=page.reason,
        sources=list(page.sources),
        matched_titles=list(page.matched_titles),
    )


def _results_out(run: SearchRun, outcome: SearchOutcome) -> SearchRunResultsOut:
    return SearchRunResultsOut(
        search_run_id=run.id,
        resume_id=run.resume_id,
        status=outcome.status,
        requested_limit=outcome.requested_limit,
        returned_count=outcome.returned_count,
        min_score=outcome.min_score,
        target_role=outcome.query_plan.role_display or None,
        domain=outcome.query_plan.domain_key,
        is_short=outcome.is_short,
        results=[_result_out(item) for item in outcome.results],
        career_pages=[_career_page_out(page) for page in outcome.career_pages],
        portals=[_portal_out(leg) for leg in run.portals],
        queries=[
            ExecutedQueryOut(
                query_text=executed.query_text,
                window=executed.window,
                result_count=executed.result_count,
                portal=leg.portal_slug,
            )
            for leg in outcome.portals
            for executed in leg.executed_queries
        ],
        warnings=list(outcome.warnings),
        summary=dict(run.summary or {}),
    )


@router.post(
    "",
    response_model=ResponseEnvelope[SearchRunResultsOut],
    status_code=status.HTTP_201_CREATED,
    summary="Find matches",
    description=(
        "Searches every selected portal for one resume and returns the "
        "qualified jobs.\n\n"
        "`requested_limit` accepts only 20, 50 or 100 and is a target for "
        "*qualified* jobs. It is split as evenly as possible over the selected "
        "portals; the remainder goes to the first portals listed. Every "
        "selected portal is searched - the run does not stop once one portal "
        "has produced enough - and a portal short of qualified jobs releases "
        "its unused slots to the others.\n\n"
        "The 70% match threshold is locked, is not accepted as input, and is "
        "never lowered to reach the requested count. If fewer jobs qualify, "
        "the smaller set is returned and `is_short` is true.\n\n"
        "Jobs already applied to **with this resume** are excluded before "
        "quotas are filled. They remain in application history, and remain "
        "available to your other resumes.\n\n"
        "Runs synchronously: each portal has a request timeout and an overall "
        "budget, so the response is bounded. One portal failing does not fail "
        "the run - it finishes `completed_with_errors` and the failure is "
        "reported in `warnings` and on that portal's row."
    ),
    responses={
        404: {"description": "Resume not found."},
        422: {
            "description": (
                "Unknown, disabled or unimplemented portal, unanalyzed "
                "resume, no target role, or an invalid limit."
            )
        },
    },
)
async def create_search_run(
    payload: SearchRunCreate,
    session: DbSession,
    settings: AppSettings,
    user: CurrentUser,
) -> ResponseEnvelope[SearchRunResultsOut]:
    resume = await resume_service.get_resume(
        session, resume_id=payload.resume_id, user_id=user.id
    )

    prepared = await search_run_service.create_search_run(
        session,
        resume=resume,
        portal_ids=payload.selected_portal_ids,
        requested_limit=payload.requested_limit,
        min_score=settings.min_match_score,
    )

    outcome = await execute_search_run(
        session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
    )
    await session.commit()

    # Re-read so the portal legs come back with their portal rows attached.
    completed = await search_run_service.get_search_run(
        session, search_run_id=prepared.run.id, user_id=user.id
    )

    if outcome.returned_count == 0:
        message = "No jobs qualified at the locked 70% threshold."
    elif outcome.is_short:
        message = (
            f"{outcome.returned_count} of {outcome.requested_limit} requested "
            "jobs qualified. The threshold is never lowered to fill the count."
        )
    else:
        message = f"{outcome.returned_count} qualified jobs found."

    return ResponseEnvelope[SearchRunResultsOut](
        data=_results_out(completed, outcome), message=message
    )


@router.get(
    "",
    response_model=ResponseEnvelope[Page[SearchRunOut]],
    summary="List search runs",
    description="Newest first. Filter by resume to see one resume's history.",
)
async def list_search_runs(
    session: DbSession,
    user: CurrentUser,
    resume_id: Annotated[uuid.UUID | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> ResponseEnvelope[Page[SearchRunOut]]:
    runs, total = await search_run_service.list_search_runs(
        session,
        user_id=user.id,
        resume_id=resume_id,
        page=page,
        page_size=page_size,
    )

    return ResponseEnvelope[Page[SearchRunOut]](
        data=Page[SearchRunOut](
            items=[_run_out(run) for run in runs],
            pagination=PaginationMeta.build(
                page=page, page_size=page_size, total=total
            ),
        )
    )


@router.get(
    "/{search_run_id}",
    response_model=ResponseEnvelope[SearchRunOut],
    summary="Get a search run",
    responses={404: {"description": "Search run not found."}},
)
async def get_search_run(
    search_run_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ResponseEnvelope[SearchRunOut]:
    run = await search_run_service.get_search_run(
        session, search_run_id=search_run_id, user_id=user.id
    )
    return ResponseEnvelope[SearchRunOut](data=_run_out(run))


@router.get(
    "/{search_run_id}/results",
    response_model=ResponseEnvelope[SearchRunResultsOut],
    summary="Get a search run's results",
    description=(
        "The qualified jobs a completed run returned, in the order it "
        "returned them.\n\n"
        "Membership and order are read back from what the run recorded, not "
        "re-derived, so a later scoring change cannot reshuffle a run that has "
        "already been shown. Application status is read live, so a job you "
        "have since applied to shows its current status here."
    ),
    responses={404: {"description": "Search run not found."}},
)
async def get_search_run_results(
    search_run_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ResponseEnvelope[SearchRunResultsOut]:
    run = await search_run_service.get_search_run(
        session, search_run_id=search_run_id, user_id=user.id
    )
    stored = await search_run_service.load_run_results(session, run=run)
    statuses = await search_run_service.application_statuses(
        session,
        resume_id=run.resume_id,
        job_ids=[item.job.id for item in stored],
    )

    summary = dict(run.summary or {})

    return ResponseEnvelope[SearchRunResultsOut](
        data=SearchRunResultsOut(
            search_run_id=run.id,
            resume_id=run.resume_id,
            status=run.status,
            requested_limit=run.requested_limit,
            returned_count=len(stored),
            min_score=run.min_score,
            target_role=summary.get("target_role") or run.searched_target_role,
            domain=summary.get("domain") or run.searched_domain,
            is_short=len(stored) < run.requested_limit,
            results=[
                _stored_result_out(item, statuses) for item in stored
            ],
            portals=[_portal_out(leg) for leg in run.portals],
            queries=[
                ExecutedQueryOut(
                    query_text=str(entry.get("query_text", "")),
                    window=entry["window"],
                    result_count=int(entry.get("result_count", 0)),
                    portal=str(entry.get("portal", "")),
                )
                for entry in summary.get("queries", []) or []
                if entry.get("window")
            ],
            warnings=list(summary.get("warnings", []) or []),
            summary=summary,
        )
    )


@router.get(
    "/{search_run_id}/progress",
    response_model=ResponseEnvelope[SearchRunProgressOut],
    summary="Get search-run progress",
    description=(
        "Per-portal progress for the dashboard.\n\n"
        "Progress is reported per portal so a failure is visibly scoped to "
        "that portal. One portal failing does not fail the run: it finishes "
        "as `completed_with_errors`."
    ),
    responses={404: {"description": "Search run not found."}},
)
async def get_search_run_progress(
    search_run_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> ResponseEnvelope[SearchRunProgressOut]:
    run = await search_run_service.get_search_run(
        session, search_run_id=search_run_id, user_id=user.id
    )
    legs = list(run.portals)

    return ResponseEnvelope[SearchRunProgressOut](
        data=SearchRunProgressOut(
            id=run.id,
            status=run.status,
            is_terminal=run.is_terminal,
            portal_count=len(legs),
            portals_completed=sum(
                1 for leg in legs if leg.status is PortalRunStatus.COMPLETED
            ),
            portals_failed=sum(
                1 for leg in legs if leg.status is PortalRunStatus.FAILED
            ),
            total_selected=sum(leg.selected_count for leg in legs),
            requested_limit=run.requested_limit,
            portals=[_portal_out(leg) for leg in legs],
        )
    )
