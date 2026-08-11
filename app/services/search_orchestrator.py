"""The complete Find Matches workflow (Phase 7, deepened in Phase 12).

    selected resume
      -> validate its analysed profile
      -> validate the selected portals
      -> generate same-role queries
      -> collect from every portal concurrently, each to its own depth
      -> normalise, apply the US-only filter, canonicalise and deduplicate
      -> exclude jobs this resume has already applied with
      -> score every remaining canonical job
      -> keep only qualified jobs scoring >= 70
      -> rank the whole candidate pool together
      -> persist the run and return the best ``requested_limit``

Everything above the persistence layer is decided by ``app.search`` and
``app.matching``; this module supplies the inputs, isolates failures and writes
the outcome.

# What Phase 12 changed, and what it deliberately did not

Before, each portal was given a quota - the requested limit divided by the
portal count - and stopped collecting the moment it had filled it. Two things
were wrong with that. A portal stopped early because of *how many other portals
were selected*, which is not a fact about that portal; and a portal holding
strong matches lost them to a weaker portal's reserved slots.

Now every portal searches independently until it is genuinely finished or hits
one of its own configured limits (``app.search.depth``), everything qualified
lands in one pool, and the pool is ranked together. A portal that finds thirty
good jobs contributes thirty.

**The ranking itself is untouched.** ``order_candidates`` and ``sort_key`` are
the same functions the quota engine used, and scoring, the 70 threshold,
canonicalisation, deduplication and the US filter are all unchanged. Only *when*
jobs reach the ranking moved.

# Guarantees this module exists to keep

* **The threshold never moves.** Only candidates that already qualified reach
  the pool. There is no code path that admits a job below 70 to reach a
  requested count - a short result set is returned instead (CLAUDE.md s18).
* **One portal failure is not a run failure.** Each leg is collected
  independently and persisted inside its own savepoint. A failed leg is recorded
  against ``search_run_portals`` and ``portal_errors`` and the run continues;
  only an all-portal failure fails the run.
* **A canonical job takes one place.** The first portal to surface a job in this
  run owns it. Later portals still get their ``job_sources`` row - the source is
  never discarded - but the job is not scored or counted twice.
* **Exclusions are applied before ranking.** A job already applied to with this
  resume is dropped after deduplication, so it cannot occupy a place in the
  returned set (Phase 6 ``eligibility``).
* **Runs do not overwrite each other.** Match rows carry ``search_run_id``, so
  each run keeps its own membership and scores, and earlier runs stay intact.

# Concurrency, and why it stops where it does

Collection is network-bound and touches no database, so all portals collect at
once and the run costs roughly the slowest portal rather than the sum of them.
Persistence is not concurrent: the legs share one ``AsyncSession``, which is not
safe for concurrent use, and processing them in the user's selection order is
what keeps attribution - and therefore the result set - reproducible. So the
work is split in two: gather in parallel, write in order.

Redis and background workers remain out of scope, so a run still executes inside
the request. Every adapter call has a timeout and every portal has a depth
budget, so the request stays bounded rather than open-ended.

# On ``job_matches`` uniqueness

Phase 6 noted that ``UNIQUE (resume_id, job_id, search_run_id)`` does not
constrain run-less rows, because PostgreSQL treats NULLs as distinct. Phase 7
always sets ``search_run_id``, so for every row written here the constraint is
fully effective and the database itself prevents a duplicate result. No
migration is needed for this phase.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ApplicationStatus,
    FreshnessWindow,
    JobUrlKind,
    PortalErrorKind,
    PortalRunStatus,
    SearchRunStatus,
)
from app.core.logging import get_logger
from app.matching.eligibility import ineligible_job_ids
from app.matching.profile import ResumeMatchProfile
from app.matching.version import SCORING_VERSION
from app.models.application import Application
from app.models.job import Job, JobMatch, JobSource
from app.models.portal import Portal
from app.models.search import PortalError as PortalErrorRow
from app.models.search import SearchQuery, SearchRun, SearchRunPortal
from app.portals.base import BasePortal
from app.portals.exceptions import (
    PortalAuthError,
    PortalBlockedError,
    PortalNotImplementedError,
    PortalNotRegisteredError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.portals.registry import registry
from app.search.allocation import (
    Candidate,
    GlobalSelection,
    select_global_top,
)
from app.search.collection import DeepCollection, ExecutedQuery, PortalCollector
from app.search.depth import ExhaustionReason, SearchDepth
from app.search.freshness import freshness_score, is_eligible_age
from app.search.queries import QueryPlan, build_query_plan
from app.processing.normalize import RejectionReason
from app.services.job_processing import process_raw_jobs
from app.services.match_service import load_match_profile, score_jobs_for_resume

logger = get_logger(__name__)

#: Listings handed to the processing layer at once. Collection now gathers a
#: whole portal before anything is written, so the batch that reaches
#: ``process_raw_jobs`` is bounded here rather than by the collector's window.
PROCESSING_CHUNK = 50

#: Recorded on every run's summary. This product is locked to United States
#: vacancies (Phase 10); the filter runs during normalisation, so a non-US
#: listing is never scored and never reaches the candidate pool.
US_ONLY_POLICY = "united_states_only"

#: Recorded on every run's summary. Phase 13: only jobs that ask for a real
#: application are returned. Quick Apply, Easy Apply and one-click listings are
#: refused during normalisation, so they are never scored and never ranked.
LONG_APPLICATION_POLICY = "long_application_only"

#: Recorded on every run's summary. Phase 12: portals search independently and
#: to their own depth, and the returned set is the Top N of one combined pool.
DEEP_UNIFIED_STRATEGY = "deep_unified"

#: Scoring is chunked so one very productive portal cannot exceed the scoring
#: service's own per-call bound.
SCORING_CHUNK = 50

#: How a portal failure is classified for ``portal_errors.kind``.
_ERROR_KINDS: tuple[tuple[type[Exception], PortalErrorKind], ...] = (
    (PortalTimeoutError, PortalErrorKind.TIMEOUT),
    (PortalRateLimitedError, PortalErrorKind.RATE_LIMITED),
    (PortalAuthError, PortalErrorKind.AUTH_REQUIRED),
    (PortalBlockedError, PortalErrorKind.BLOCKED),
    (PortalParseError, PortalErrorKind.PARSE),
    (PortalNotRegisteredError, PortalErrorKind.NOT_IMPLEMENTED),
    (PortalNotImplementedError, PortalErrorKind.NOT_IMPLEMENTED),
)

#: Direct-URL quality, 0-100, for the stored ranking blend (CLAUDE.md s8).
_URL_QUALITY: dict[JobUrlKind, float] = {
    JobUrlKind.EMPLOYER_CAREER: 100.0,
    JobUrlKind.ATS: 75.0,
    JobUrlKind.PORTAL_LISTING: 50.0,
    JobUrlKind.AGGREGATOR_REDIRECT: 25.0,
    JobUrlKind.UNKNOWN: 0.0,
}

# Final dashboard ranking weights (CLAUDE.md section 8).
RANKING_MATCH_WEIGHT = 0.75
RANKING_FRESHNESS_WEIGHT = 0.20
RANKING_SOURCE_WEIGHT = 0.05


def classify_error(exc: BaseException) -> PortalErrorKind:
    """Map an exception onto the coarse, portal-agnostic failure taxonomy."""
    for error_type, kind in _ERROR_KINDS:
        if isinstance(exc, error_type):
            return kind
    if isinstance(exc, TimeoutError):
        return PortalErrorKind.TIMEOUT
    return PortalErrorKind.UNKNOWN


@dataclass(slots=True)
class PortalLegOutcome:
    """What one portal contributed, and what went wrong if anything."""

    portal_id: uuid.UUID
    portal_slug: str
    portal_display_name: str
    index: int
    status: PortalRunStatus
    target_quota: int
    collected_count: int = 0
    normalized_count: int = 0
    #: Listings dropped by the US-only location filter (Phase 10). Counted
    #: separately from other normalisation failures so the run can say plainly
    #: how much of what a portal returned was outside the United States.
    non_us_count: int = 0
    #: Listings refused by the application-type filter (Phase 13) - one-click
    #: and Easy Apply jobs. Counted separately from other normalisation
    #: failures so the run can say plainly how much of what a portal returned
    #: was not a real application.
    quick_apply_count: int = 0
    duplicate_count: int = 0
    excluded_count: int = 0
    evaluated_count: int = 0
    qualified_count: int = 0
    selected_count: int = 0
    deepest_window: FreshnessWindow | None = None
    #: Why this portal stopped collecting (Phase 12). EXHAUSTED means it had
    #: nothing further to give; anything else means a configured limit or a
    #: failure ended the leg early, which the summary reports rather than hides.
    exhaustion: ExhaustionReason | None = None
    #: Depth actually reached, for the run summary.
    requests: int = 0
    pages: int = 0
    collection_seconds: float = 0.0
    error: str | None = None
    error_kind: PortalErrorKind | None = None
    executed_queries: list[ExecutedQuery] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status is PortalRunStatus.FAILED


@dataclass(slots=True)
class SearchResultItem:
    """One qualified job in the returned result set."""

    job_id: uuid.UUID
    title: str
    company: str | None
    location: str | None
    work_mode: str | None
    posted_at: datetime | None
    final_score: float
    ranking_score: float
    freshness_score: float
    qualified: bool
    role_verdict: str
    match_explanation: str
    positive_evidence: tuple[str, ...]
    portal_slugs: tuple[str, ...]
    discovered_via: str
    preferred_application_url: str | None
    preferred_url_kind: str | None
    application_status: ApplicationStatus
    is_new: bool


@dataclass(slots=True)
class SearchOutcome:
    """The complete result of executing one search run."""

    search_run_id: uuid.UUID
    status: SearchRunStatus
    requested_limit: int
    min_score: int
    query_plan: QueryPlan
    results: list[SearchResultItem] = field(default_factory=list)
    portals: list[PortalLegOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selection: GlobalSelection | None = None

    @property
    def returned_count(self) -> int:
        return len(self.results)

    @property
    def is_short(self) -> bool:
        """True when fewer jobs qualified than the user asked for."""
        return self.returned_count < self.requested_limit


def adapter_options(portal: Portal) -> dict[str, Any]:
    """Build an adapter's options from its database configuration.

    Operational policy comes from ``portal_configs`` rather than from code, so
    tuning a portal never means editing its adapter (CLAUDE.md section 4). The
    adapter's own opaque ``options`` win: they may deliberately override a
    default, and the core does not inspect their meaning.
    """
    config = getattr(portal, "config", None)
    options: dict[str, Any] = {}

    if config is not None:
        options.update(
            {
                "timeout_seconds": config.timeout_seconds,
                "max_retries": config.max_retries,
                "retry_backoff_seconds": config.retry_backoff_seconds,
                "rate_limit_per_minute": config.rate_limit_per_minute,
            }
        )
        options.update(dict(config.options or {}))

    return options


def build_adapter(portal: Portal) -> BasePortal:
    """Resolve one portal row to a live adapter through the registry.

    The single indirection that keeps portal names out of this service. There
    is no ``if portal == ...`` chain anywhere in this module.
    """
    key = portal.adapter_key or portal.slug
    return registry.create(key, adapter_options(portal))


async def execute_search_run(
    session: AsyncSession,
    *,
    run: SearchRun,
    legs: list[SearchRunPortal],
    portals: list[Portal],
    adapters: dict[uuid.UUID, BasePortal] | None = None,
    now: datetime | None = None,
    query_timeout_seconds: float | None = None,
    portal_budget_seconds: float | None = None,
) -> SearchOutcome:
    """Run the complete Find Matches workflow for one already-created run.

    ``legs`` and ``portals`` are positionally aligned and in the user's
    selection order, which is the order quota remainders follow.

    ``adapters`` lets a caller supply pre-built adapters. Tests use it to inject
    deterministic fixture portals; in normal operation it is None and each
    portal is resolved through the registry.
    """
    moment = now or datetime.now(UTC)

    run.status = SearchRunStatus.RUNNING
    run.started_at = moment
    await session.flush()

    profile = await load_match_profile(session, resume_id=run.resume_id)
    plan = build_query_plan(profile)

    outcome = SearchOutcome(
        search_run_id=run.id,
        status=SearchRunStatus.RUNNING,
        requested_limit=run.requested_limit,
        min_score=run.min_score,
        query_plan=plan,
    )

    if plan.is_empty:
        # Nothing searchable. Fail honestly rather than issuing a broad query.
        return await _finish_without_collection(
            session,
            run=run,
            legs=legs,
            outcome=outcome,
            moment=moment,
            message=(
                "No same-role search queries could be generated for this "
                "resume's target role."
            ),
        )

    # One query, once per run, before any selection (Phase 6 eligibility).
    excluded = await ineligible_job_ids(session, resume_id=run.resume_id)

    # --- Stage 1: read everything the concurrent stage will need ------------
    # Adapter options come off the ORM session, so they are resolved here,
    # sequentially, before anything runs in parallel.
    plans: list[_LegPlan] = []
    for index, (leg, portal) in enumerate(zip(legs, portals, strict=True)):
        leg.status = PortalRunStatus.RUNNING
        leg.started_at = moment
        options = adapter_options(portal)
        plans.append(
            _LegPlan(
                index=index,
                leg=leg,
                portal=portal,
                adapter=(adapters or {}).get(portal.id),
                options=options,
                depth=SearchDepth.from_options(options),
            )
        )
    await session.flush()

    # --- Stage 2: collect from every portal, concurrently -------------------
    # Nothing in here touches the session. Portals no longer wait on each
    # other, and none of them stops because another already had enough.
    collections = await asyncio.gather(
        *(
            _collect_leg(
                leg_plan,
                queries=plan.queries,
                moment=moment,
                query_timeout_seconds=query_timeout_seconds,
                portal_budget_seconds=portal_budget_seconds,
            )
            for leg_plan in plans
        )
    )

    # --- Stage 3: persist and score, one portal at a time -------------------
    # Back on a single session, in the user's selection order, so attribution -
    # and therefore the result - is reproducible.
    attribution: dict[uuid.UUID, int] = {}
    for leg_plan, collected in zip(plans, collections, strict=True):
        leg_outcome = await _process_leg(
            session,
            run=run,
            plan=leg_plan,
            collected=collected,
            excluded=excluded,
            attribution=attribution,
            moment=moment,
        )
        outcome.portals.append(leg_outcome)

    await _finalise(
        session, run=run, legs=legs, outcome=outcome, moment=moment
    )
    return outcome


# ---------------------------------------------------------------------------
# One portal's leg
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _LegPlan:
    """Everything one portal's leg needs, read off the session up front."""

    index: int
    leg: SearchRunPortal
    portal: Portal
    adapter: BasePortal | None
    options: dict[str, Any]
    depth: SearchDepth


async def _collect_leg(
    plan: _LegPlan,
    *,
    queries: tuple[str, ...],
    moment: datetime,
    query_timeout_seconds: float | None,
    portal_budget_seconds: float | None,
) -> DeepCollection:
    """Collect one portal's listings. No database, safe to run concurrently.

    Builds the adapter here rather than earlier so a portal whose adapter
    cannot be resolved fails as a portal failure - isolated to its own leg -
    instead of taking down the whole run before collection starts.
    """
    if plan.adapter is None:
        try:
            adapter = build_adapter(plan.portal)
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            collected = DeepCollection(portal_slug=plan.portal.slug)
            collected.reason = ExhaustionReason.ERROR
            collected.error = exc
            return collected
    else:
        adapter = plan.adapter

    collector = PortalCollector(
        adapter,
        queries=queries,
        # Depth is now the portal's own configuration. It is deliberately not
        # derived from the requested limit or the portal count: what one portal
        # collects must not depend on how many others were selected. An explicit
        # argument still wins, so a caller can bound a run tightly.
        per_window_limit=plan.depth.per_request_limit,
        query_timeout_seconds=(
            query_timeout_seconds
            if query_timeout_seconds is not None
            else plan.depth.query_timeout_seconds
        ),
        budget_seconds=(
            portal_budget_seconds
            if portal_budget_seconds is not None
            else plan.depth.max_runtime_seconds
        ),
    )

    try:
        return await collector.collect(now=moment, depth=plan.depth)
    except Exception as exc:  # noqa: BLE001 - a broken adapter stays isolated
        collected = DeepCollection(portal_slug=plan.portal.slug)
        collected.reason = ExhaustionReason.ERROR
        collected.error = exc
        return collected


async def _process_leg(
    session: AsyncSession,
    *,
    run: SearchRun,
    plan: _LegPlan,
    collected: DeepCollection,
    excluded: frozenset[uuid.UUID],
    attribution: dict[uuid.UUID, int],
    moment: datetime,
) -> PortalLegOutcome:
    """Persist and score one portal's collection, isolated from the others.

    Every failure path ends with this leg marked FAILED and the run carrying
    on. The savepoint means a partially written leg cannot leave the run's own
    bookkeeping inconsistent.
    """
    leg, portal, index = plan.leg, plan.portal, plan.index

    result = PortalLegOutcome(
        portal_id=portal.id,
        portal_slug=portal.slug,
        portal_display_name=portal.display_name,
        index=index,
        status=PortalRunStatus.RUNNING,
        target_quota=leg.target_quota,
        deepest_window=collected.deepest_window,
        exhaustion=collected.reason,
        requests=collected.requests,
        pages=collected.pages,
        collection_seconds=collected.elapsed_seconds,
    )

    savepoint = await session.begin_nested()
    try:
        if collected.error is not None:
            # Collection produced nothing usable. Raised here so this leg takes
            # the same failure path as a processing error.
            raise collected.error

        for executed in collected.executed:
            result.executed_queries.append(executed)
            session.add(
                SearchQuery(
                    search_run_portal_id=leg.id,
                    query_text=executed.query_text[:256],
                    window=executed.window,
                    page=executed.page,
                    result_count=executed.result_count,
                    executed_at=executed.executed_at,
                    duration_ms=executed.duration_ms,
                )
            )

        result.collected_count = len(collected.jobs)

        for start in range(0, len(collected.jobs), PROCESSING_CHUNK):
            batch = collected.jobs[start : start + PROCESSING_CHUNK]

            processed = await process_raw_jobs(
                session,
                portal=portal,
                raw_jobs=batch,
                search_run_portal=leg,
                now=moment,
            )
            result.normalized_count += len(processed.processed)
            result.non_us_count += sum(
                1
                for rejected in processed.rejected
                if rejected.reason == RejectionReason.NON_US_LOCATION
            )
            result.quick_apply_count += sum(
                1
                for rejected in processed.rejected
                if rejected.reason == RejectionReason.QUICK_APPLY
            )

            # Jobs another portal already claimed in this run. Their source row
            # was still written above - the listing is never discarded, it just
            # is not scored or counted twice.
            fresh_ids: list[uuid.UUID] = []
            for job_id in processed.canonical_job_ids:
                if job_id in attribution:
                    result.duplicate_count += 1
                    continue
                fresh_ids.append(job_id)

            # Applied-with-this-resume jobs leave before ranking, so they
            # cannot occupy a place in the returned set.
            eligible_ids = [job_id for job_id in fresh_ids if job_id not in excluded]
            result.excluded_count += len(fresh_ids) - len(eligible_ids)

            if eligible_ids:
                await _score_and_collect(
                    session,
                    run=run,
                    job_ids=eligible_ids,
                    index=index,
                    attribution=attribution,
                    result=result,
                    moment=moment,
                )

            await session.flush()

        result.status = PortalRunStatus.COMPLETED
        await savepoint.commit()

    except Exception as exc:  # noqa: BLE001 - isolation is the point
        await savepoint.rollback()

        result.status = PortalRunStatus.FAILED
        result.error = str(exc)
        result.error_kind = classify_error(exc)
        # The savepoint rolled back this leg's collected work, so its counters
        # would otherwise claim results that no longer exist.
        result.candidates = []
        result.collected_count = 0
        result.normalized_count = 0
        # Reset with the rest: a failed leg that reported "1 listing outside the
        # US" alongside "0 collected" is a contradiction the summary should not
        # print.
        result.non_us_count = 0
        result.quick_apply_count = 0
        result.duplicate_count = 0
        result.excluded_count = 0
        result.evaluated_count = 0
        result.qualified_count = 0
        for job_id in [
            job_id for job_id, owner in attribution.items() if owner == index
        ]:
            attribution.pop(job_id, None)

        logger.warning(
            "search_portal_leg_failed",
            search_run_id=str(run.id),
            portal=portal.slug,
            kind=result.error_kind.value,
            error=result.error,
        )

        session.add(
            PortalErrorRow(
                portal_id=portal.id,
                search_run_id=run.id,
                search_run_portal_id=leg.id,
                kind=result.error_kind,
                message=result.error[:2000],
                context={"phase": "collection", "portal": portal.slug},
            )
        )

    result.qualified_count = len(result.candidates)

    leg.status = result.status
    leg.raw_count = result.collected_count
    leg.qualified_count = result.qualified_count
    leg.deepest_window = result.deepest_window
    leg.error = result.error
    leg.completed_at = datetime.now(UTC)
    await session.flush()

    logger.info(
        "search_portal_leg_completed",
        search_run_id=str(run.id),
        portal=portal.slug,
        status=result.status.value,
        collected=result.collected_count,
        qualified=result.qualified_count,
        excluded=result.excluded_count,
        duplicates=result.duplicate_count,
    )

    return result


async def _score_and_collect(
    session: AsyncSession,
    *,
    run: SearchRun,
    job_ids: list[uuid.UUID],
    index: int,
    attribution: dict[uuid.UUID, int],
    result: PortalLegOutcome,
    moment: datetime,
) -> None:
    """Score a batch of canonical jobs and keep the qualified ones."""
    for start in range(0, len(job_ids), SCORING_CHUNK):
        chunk = job_ids[start : start + SCORING_CHUNK]

        jobs = list(
            (await session.scalars(select(Job).where(Job.id.in_(chunk)))).all()
        )
        if not jobs:
            continue

        summary = await score_jobs_for_resume(
            session,
            resume_id=run.resume_id,
            jobs=jobs,
            persist=True,
            search_run_id=run.id,
            now=moment,
        )
        result.evaluated_count += len(summary.scored)

        by_id = {job.id: job for job in jobs}

        for scored in summary.scored:
            # Claimed whether or not it qualified: a rejected job must not be
            # re-scored by the next portal in the same run.
            attribution[scored.job_id] = index

            if not scored.is_presentable:
                continue

            job = by_id[scored.job_id]

            # The age gate, applied here rather than only at collection so it
            # holds however the job arrived - a rediscovery, a cross-portal
            # duplicate, or a listing whose date was corrected after it was
            # first stored. A job past the window is not eligible at any score.
            if not is_eligible_age(job.posted_at, moment):
                continue

            result.candidates.append(
                Candidate(
                    job_id=scored.job_id,
                    portal_index=index,
                    final_score=scored.final_score,
                    posted_at=job.posted_at,
                    role_verdict=scored.result.role.verdict,
                    skill_coverage=scored.result.skills.required_coverage,
                    canonical_key=job.canonical_key,
                )
            )


# ---------------------------------------------------------------------------
# Finishing the run
# ---------------------------------------------------------------------------


async def _finish_without_collection(
    session: AsyncSession,
    *,
    run: SearchRun,
    legs: list[SearchRunPortal],
    outcome: SearchOutcome,
    moment: datetime,
    message: str,
) -> SearchOutcome:
    """End a run that could not legitimately start."""
    for leg in legs:
        leg.status = PortalRunStatus.SKIPPED
        leg.completed_at = moment

    run.status = SearchRunStatus.FAILED
    run.completed_at = moment
    run.error_message = message
    run.summary = {
        "requested_limit": run.requested_limit,
        "returned_count": 0,
        "queries": [],
        "warnings": [message],
        "threshold": run.min_score,
        "scoring_version": SCORING_VERSION,
        "location_policy": US_ONLY_POLICY,
        "non_us_excluded_count": 0,
        "application_policy": LONG_APPLICATION_POLICY,
        "quick_apply_excluded_count": 0,
        "search_strategy": DEEP_UNIFIED_STRATEGY,
        "candidate_pool_size": 0,
        "portals_exhausted": 0,
        "portals_limited": [],
        "total_requests": 0,
    }
    await session.flush()

    outcome.status = SearchRunStatus.FAILED
    outcome.warnings.append(message)
    return outcome


async def _finalise(
    session: AsyncSession,
    *,
    run: SearchRun,
    legs: list[SearchRunPortal],
    outcome: SearchOutcome,
    moment: datetime,
) -> None:
    """Rank the whole candidate pool and record the run's outcome.

    Phase 12: there are no per-portal quotas here. Every qualified job from
    every portal is ranked together and the best ``requested_limit`` are
    returned, so a portal that found thirty strong matches contributes thirty
    and one that found two contributes two.
    """
    candidates = [
        candidate for leg in outcome.portals for candidate in leg.candidates
    ]

    selection = select_global_top(
        candidates,
        portal_count=len(legs),
        requested_limit=run.requested_limit,
    )
    outcome.selection = selection

    for leg, leg_outcome, selected in zip(
        legs,
        outcome.portals,
        selection.per_portal_selected,
        strict=True,
    ):
        # No quota gates anything now; the column records what this portal
        # actually contributed to the returned set.
        leg.final_quota = selected
        leg.selected_count = selected
        leg_outcome.selected_count = selected

    outcome.results = await _build_results(
        session, run=run, selection=selection, outcome=outcome, moment=moment
    )

    failed = [leg for leg in outcome.portals if leg.failed]
    if failed and len(failed) == len(outcome.portals):
        run.status = SearchRunStatus.FAILED
        run.error_message = (
            "Every selected portal failed: "
            + "; ".join(f"{leg.portal_slug}: {leg.error}" for leg in failed)
        )[:4000]
    elif failed:
        run.status = SearchRunStatus.COMPLETED_WITH_ERRORS
    else:
        run.status = SearchRunStatus.COMPLETED

    outcome.status = run.status

    for leg in failed:
        outcome.warnings.append(
            f"{leg.portal_display_name} failed ({leg.error_kind.value if leg.error_kind else 'unknown'}): {leg.error}"
        )

    # Say it on every run, not only when something was dropped: the user needs
    # to know the search was US-only whether or not it changed the outcome.
    excluded = sum(leg.non_us_count for leg in outcome.portals)
    if run.status is not SearchRunStatus.FAILED:
        note = "Only United States jobs were considered."
        if excluded:
            note += (
                f" {excluded} listing{'s' if excluded != 1 else ''} outside the "
                "US were excluded before scoring."
            )
        outcome.warnings.append(note)

    # Likewise on every run: the user is entitled to know the search excluded
    # one-click applications whether or not any turned up.
    quick_apply = sum(leg.quick_apply_count for leg in outcome.portals)
    if run.status is not SearchRunStatus.FAILED:
        note = "Only jobs with a real application process were considered."
        if quick_apply:
            note += (
                f" {quick_apply} Quick Apply or Easy Apply "
                f"listing{'s' if quick_apply != 1 else ''} "
                "were excluded before scoring."
            )
        outcome.warnings.append(note)

    if outcome.is_short and run.status is not SearchRunStatus.FAILED:
        outcome.warnings.append(
            f"{outcome.returned_count} of {run.requested_limit} requested jobs "
            f"qualified at the locked {run.min_score}% threshold. The "
            "threshold is never lowered to fill the count."
        )

    run.completed_at = datetime.now(UTC)
    run.summary = _summary(run=run, outcome=outcome, selection=selection)
    await session.flush()

    logger.info(
        "search_run_completed",
        search_run_id=str(run.id),
        status=run.status.value,
        requested=run.requested_limit,
        returned=outcome.returned_count,
        portals=len(outcome.portals),
        failed_portals=len(failed),
    )


def _summary(
    *,
    run: SearchRun,
    outcome: SearchOutcome,
    selection: GlobalSelection,
) -> dict[str, Any]:
    """The run's own record of what happened.

    ``selected_job_ids`` is this run's result membership, in the returned
    order. Kept here rather than in a new column: ``search_runs.summary`` was
    added in Phase 3 precisely so Phase 7 could record its reasoning without a
    migration, and the ordering is reproducible from it.
    """
    return {
        "requested_limit": run.requested_limit,
        "returned_count": outcome.returned_count,
        "threshold": run.min_score,
        "scoring_version": SCORING_VERSION,
        "target_role": outcome.query_plan.role_display,
        "domain": outcome.query_plan.domain_key,
        # This product considers United States vacancies only, and says so on
        # every run rather than leaving the user to infer it from the results.
        "location_policy": US_ONLY_POLICY,
        "non_us_excluded_count": sum(leg.non_us_count for leg in outcome.portals),
        # Phase 13: this product returns only jobs asking for a real
        # application, and says how many one-click listings that cost.
        "application_policy": LONG_APPLICATION_POLICY,
        "quick_apply_excluded_count": sum(
            leg.quick_apply_count for leg in outcome.portals
        ),
        "collected_count": sum(leg.collected_count for leg in outcome.portals),
        "normalized_count": sum(leg.normalized_count for leg in outcome.portals),
        "duplicate_count": sum(leg.duplicate_count for leg in outcome.portals),
        "excluded_count": sum(leg.excluded_count for leg in outcome.portals),
        "evaluated_count": sum(leg.evaluated_count for leg in outcome.portals),
        "qualified_count": sum(leg.qualified_count for leg in outcome.portals),
        # Phase 12: every qualified job from every portal competes in one pool
        # on the same ranking. There are no per-portal quotas to redistribute.
        "search_strategy": DEEP_UNIFIED_STRATEGY,
        "candidate_pool_size": selection.pool_size,
        "portals_exhausted": sum(
            1
            for leg in outcome.portals
            if leg.exhaustion is not None and leg.exhaustion.is_complete
        ),
        "portals_limited": sorted(
            {
                leg.exhaustion.value
                for leg in outcome.portals
                if leg.exhaustion is not None and not leg.exhaustion.is_complete
            }
        ),
        "total_requests": sum(leg.requests for leg in outcome.portals),
        "queries": [
            {
                "query_text": executed.query_text,
                "window": executed.window.value,
                "result_count": executed.result_count,
                "portal": leg.portal_slug,
            }
            for leg in outcome.portals
            for executed in leg.executed_queries
        ],
        "portals": [
            {
                "portal": leg.portal_slug,
                "status": leg.status.value,
                "collected": leg.collected_count,
                "normalized": leg.normalized_count,
                "qualified": leg.qualified_count,
                "selected": leg.selected_count,
                "non_us_excluded": leg.non_us_count,
                "quick_apply_excluded": leg.quick_apply_count,
                "duplicates": leg.duplicate_count,
                # How deep this portal actually went, and why it stopped.
                "exhaustion": (
                    leg.exhaustion.value if leg.exhaustion else None
                ),
                "exhausted": (
                    leg.exhaustion.is_complete if leg.exhaustion else False
                ),
                "requests": leg.requests,
                "pages": leg.pages,
                "collection_seconds": leg.collection_seconds,
                "deepest_window": (
                    leg.deepest_window.value if leg.deepest_window else None
                ),
                "error": leg.error,
                "error_kind": leg.error_kind.value if leg.error_kind else None,
            }
            for leg in outcome.portals
        ],
        "selected_job_ids": [str(item.job_id) for item in outcome.results],
        "warnings": list(outcome.warnings),
    }


async def _build_results(
    session: AsyncSession,
    *,
    run: SearchRun,
    selection: GlobalSelection,
    outcome: SearchOutcome,
    moment: datetime,
) -> list[SearchResultItem]:
    """Assemble the returned rows, and finish each match's ranking score."""
    if not selection.selected:
        return []

    job_ids = [candidate.job_id for candidate in selection.selected]

    jobs = {
        job.id: job
        for job in (
            await session.scalars(select(Job).where(Job.id.in_(job_ids)))
        ).all()
    }

    matches = {
        match.job_id: match
        for match in (
            await session.scalars(
                select(JobMatch).where(
                    JobMatch.search_run_id == run.id,
                    JobMatch.job_id.in_(job_ids),
                )
            )
        ).all()
    }

    statuses = {
        job_id: status
        for job_id, status in await session.execute(
            select(Application.job_id, Application.status).where(
                Application.resume_id == run.resume_id,
                Application.job_id.in_(job_ids),
            )
        )
    }

    slugs_by_job = await portal_slugs_for_jobs(session, job_ids=job_ids)
    portal_by_index = {leg.index: leg.portal_slug for leg in outcome.portals}

    items: list[SearchResultItem] = []

    for candidate in selection.selected:
        job = jobs.get(candidate.job_id)
        if job is None:  # pragma: no cover - defensive
            continue

        match = matches.get(candidate.job_id)
        recency = freshness_score(job.posted_at, moment)
        source_quality = _URL_QUALITY.get(
            job.preferred_url_kind or JobUrlKind.UNKNOWN, 0.0
        )
        ranking = round(
            RANKING_MATCH_WEIGHT * candidate.final_score
            + RANKING_FRESHNESS_WEIGHT * recency
            + RANKING_SOURCE_WEIGHT * source_quality,
            4,
        )

        details = dict(match.score_details or {}) if match is not None else {}
        evidence = tuple(details.get("positive_evidence", ()) or ())
        role_reason = str((details.get("role") or {}).get("reason", ""))

        if match is not None:
            # Phase 6 leaves these at the plain score: only a run knows the
            # freshness and source context that section 8's blend needs.
            match.freshness_score = recency
            match.ranking_score = ranking

        items.append(
            SearchResultItem(
                job_id=job.id,
                title=job.title,
                company=job.company,
                location=job.location,
                work_mode=job.work_mode.value if job.work_mode else None,
                posted_at=job.posted_at,
                final_score=candidate.final_score,
                ranking_score=ranking,
                freshness_score=recency,
                qualified=True,
                role_verdict=candidate.role_verdict.value,
                match_explanation=role_reason,
                positive_evidence=evidence,
                portal_slugs=slugs_by_job.get(job.id, ()),
                discovered_via=portal_by_index.get(candidate.portal_index, ""),
                preferred_application_url=job.preferred_application_url,
                preferred_url_kind=(
                    job.preferred_url_kind.value if job.preferred_url_kind else None
                ),
                application_status=statuses.get(
                    job.id, ApplicationStatus.NOT_APPLIED
                ),
                is_new=job.first_discovered_at >= run.started_at
                if run.started_at
                else False,
            )
        )

    await session.flush()
    return items


async def portal_slugs_for_jobs(
    session: AsyncSession, *, job_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, ...]]:
    """Every portal that carries each job, so the source column is complete.

    A job found through three portals shows all three: merging must never hide
    which portals surfaced it (CLAUDE.md section 9).
    """
    rows = await session.execute(
        select(JobSource.job_id, Portal.slug)
        .join(Portal, Portal.id == JobSource.portal_id)
        .where(JobSource.job_id.in_(job_ids))
        .order_by(Portal.slug)
    )

    grouped: dict[uuid.UUID, list[str]] = {}
    for job_id, slug in rows:
        grouped.setdefault(job_id, []).append(slug)

    return {job_id: tuple(slugs) for job_id, slugs in grouped.items()}


__all__ = [
    "DEEP_UNIFIED_STRATEGY",
    "PROCESSING_CHUNK",
    "US_ONLY_POLICY",
    "PortalLegOutcome",
    "SearchOutcome",
    "SearchResultItem",
    "adapter_options",
    "build_adapter",
    "classify_error",
    "execute_search_run",
]
