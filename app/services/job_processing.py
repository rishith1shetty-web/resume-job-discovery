"""Turn collected listings into canonical jobs, sources and duplicate decisions.

The only place Phase 5's rules meet the database. Everything it decides comes
from ``app.processing``; this module loads candidates, applies the verdict and
writes the result.

Guarantees it is built to keep:

* **Every source is preserved.** A duplicate is attached to the canonical job
  as another ``job_sources`` row, never dropped. Choosing one preferred URL
  never discards the others (CLAUDE.md section 9).
* **Processing is idempotent.** Running the same listings twice creates no
  second canonical job and no second source row.
* **Application history is untouched.** Merging changes ``jobs`` and
  ``job_sources`` only. Status lives on ``applications``, keyed by
  (user, resume, job), and nothing here writes to it.
* **One portal's failure is isolated.** A listing that cannot be normalised is
  recorded as rejected and the batch continues (CLAUDE.md section 18).

Not in scope, by design: resume scoring, the 70% gate, ranking, quotas and
search orchestration. This produces the jobs those phases will operate on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.constants import JOB_URL_PREFERENCE, JobUrlKind, RawJobStatus
from app.core.logging import get_logger
from app.models.job import Job, JobSource, RawJobRecord
from app.models.portal import Portal
from app.portals.base import RawJob
from app.processing.dedup import DuplicateVerdict, ExistingJobView, best_match
from app.processing.locations import normalize_location
from app.processing.merge import merge_into_existing
from app.processing.normalize import (
    NormalizedJob,
    RejectedJob,
    normalize_raw_job,
)
from app.processing.titles import normalize_title
from app.processing.urls import canonical_url

if TYPE_CHECKING:
    from app.models.search import SearchRunPortal

logger = get_logger(__name__)

#: How many existing jobs to consider as duplicate candidates for one listing.
#: A cap keeps a common title at a large employer from loading thousands of
#: rows; candidates are the narrow, indexed matches, so overflow is unlikely.
MAX_DUPLICATE_CANDIDATES = 50


@dataclass(slots=True)
class ProcessedJob:
    """What happened to one collected listing."""

    portal_slug: str
    external_id: str | None
    title: str
    job_id: UUID
    canonical_key: str
    #: True when this listing created the canonical job rather than joining one.
    created_job: bool
    #: True when a source row was added for this portal.
    created_source: bool
    verdict: DuplicateVerdict | None
    changed_fields: tuple[str, ...] = ()
    preferred_application_url: str | None = None
    preferred_url_kind: JobUrlKind | None = None


@dataclass(slots=True)
class ProcessingResult:
    """Outcome of processing one portal's batch."""

    portal_slug: str
    processed: list[ProcessedJob] = field(default_factory=list)
    rejected: list[RejectedJob] = field(default_factory=list)

    @property
    def jobs_created(self) -> int:
        return sum(1 for item in self.processed if item.created_job)

    @property
    def sources_created(self) -> int:
        return sum(1 for item in self.processed if item.created_source)

    @property
    def duplicates(self) -> int:
        return sum(
            1
            for item in self.processed
            if item.verdict is not None and item.verdict.is_duplicate
        )

    @property
    def canonical_job_ids(self) -> tuple[UUID, ...]:
        seen: dict[UUID, None] = {}
        for item in self.processed:
            seen.setdefault(item.job_id, None)
        return tuple(seen)


def _existing_view(job: Job, sources: list[JobSource]) -> ExistingJobView | None:
    """Re-derive the comparison values for a stored job.

    Read back through the same normalisers that produced them, so a job stored
    by an earlier version of the rules is compared under the current ones
    rather than against a stale key.
    """
    title = normalize_title(job.title)
    if title is None:
        # Nothing comparable survives, so this job cannot be a duplicate
        # candidate. Skipping it means a new job may be created; that is the
        # safe direction.
        return None

    location = normalize_location(job.location)

    source_urls: set[str] = set()
    application_urls: set[str] = set()
    external_ids: set[tuple[str, str]] = set()

    for source in sources:
        canonical_source = canonical_url(source.source_url)
        if canonical_source:
            source_urls.add(canonical_source)

        canonical_application = canonical_url(source.application_url)
        if canonical_application and source.url_kind in (
            JobUrlKind.ATS,
            JobUrlKind.EMPLOYER_CAREER,
        ):
            application_urls.add(canonical_application)

        if source.external_id and source.portal is not None:
            external_ids.add((source.portal.slug, source.external_id))

    return ExistingJobView(
        job_id=job.id,
        canonical_key=job.canonical_key,
        title=title,
        company_key=job.normalized_company or "",
        company_display=job.company,
        location=location,
        posted_at=job.posted_at,
        description_fingerprint=job.description_fingerprint,
        source_canonical_urls=frozenset(source_urls),
        application_canonical_urls=frozenset(application_urls),
        external_ids=frozenset(external_ids),
    )


async def _load_candidates(
    session: AsyncSession, candidate: NormalizedJob
) -> list[tuple[Job, list[JobSource]]]:
    """Find jobs worth comparing against, using indexed lookups only.

    Four ways in, matching the signals in CLAUDE.md section 9: the canonical
    key, the employer, the description fingerprint, and any source URL this
    listing carries. Anything the comparison rules could accept has to be
    reachable through one of them.
    """
    filters = [Job.canonical_key == candidate.canonical_key]

    if candidate.company_key:
        filters.append(Job.normalized_company == candidate.company_key)
    if candidate.description_fingerprint:
        filters.append(
            Job.description_fingerprint == candidate.description_fingerprint
        )

    statement = (
        select(Job)
        .where(or_(*filters))
        .options(selectinload(Job.sources).selectinload(JobSource.portal))
        .limit(MAX_DUPLICATE_CANDIDATES)
    )
    jobs = list((await session.scalars(statement)).all())

    # Jobs reachable only through a shared URL: a different employer spelling
    # would keep them out of the filters above. Matched on the stored URL
    # forms, which is what ``job_sources`` actually holds.
    known_ids = {job.id for job in jobs}
    stored_urls = candidate.candidate_stored_urls
    url_statement = (
        select(Job)
        .join(JobSource, JobSource.job_id == Job.id)
        .where(
            or_(
                JobSource.source_url.in_(stored_urls),
                JobSource.application_url.in_(stored_urls),
            )
        )
        .options(selectinload(Job.sources).selectinload(JobSource.portal))
        .limit(MAX_DUPLICATE_CANDIDATES)
    )
    for job in (await session.scalars(url_statement)).all():
        if job.id not in known_ids:
            jobs.append(job)
            known_ids.add(job.id)

    return [(job, list(job.sources)) for job in jobs]


def _resolve_preferred_url(sources: list[JobSource]) -> tuple[str | None, JobUrlKind | None]:
    """Pick the most direct usable application URL across every source.

    Order is ``JOB_URL_PREFERENCE`` - employer, then ATS, then the portal's own
    listing, then an aggregator hop (CLAUDE.md section 9). Ties break on the
    source's first-seen time and then the URL itself. Not on the row id: that
    is generated at flush time, so a pending source compares as None and the
    chosen URL would change the moment it was written. Nothing is discarded -
    the losing sources keep their own URLs.
    """
    best: tuple[tuple[int, datetime, str], JobSource] | None = None

    for source in sources:
        url = source.application_url or source.source_url
        if not url:
            continue

        rank = (
            JOB_URL_PREFERENCE[source.url_kind],
            source.first_seen_at or datetime.now(UTC),
            url,
        )
        if best is None or rank < best[0]:
            best = (rank, source)

    if best is None:
        return None, None

    winner = best[1]
    for source in sources:
        source.is_preferred = source is winner

    return (winner.application_url or winner.source_url), winner.url_kind


async def process_raw_jobs(
    session: AsyncSession,
    *,
    portal: Portal,
    raw_jobs: list[RawJob],
    search_run_portal: SearchRunPortal | None = None,
    now: datetime | None = None,
) -> ProcessingResult:
    """Normalise, deduplicate and store one portal's collected listings.

    ``search_run_portal`` is optional. When supplied, each capture is also
    written to ``raw_jobs`` as an audit trail with its processing status; when
    omitted - as in a controlled verification run - only canonical jobs and
    their sources are written. Nothing here creates or advances a search run:
    that is Phase 7.
    """
    result = ProcessingResult(portal_slug=portal.slug)
    moment = now or datetime.now(UTC)

    for raw in raw_jobs:
        normalized = normalize_raw_job(raw, now=moment)

        if isinstance(normalized, RejectedJob):
            result.rejected.append(normalized)
            if search_run_portal is not None:
                session.add(
                    _raw_record(
                        raw,
                        portal=portal,
                        search_run_portal=search_run_portal,
                        status=RawJobStatus.REJECTED,
                        rejection_reason=normalized.reason,
                    )
                )
            logger.info(
                "job_processing_rejected",
                portal=portal.slug,
                reason=normalized.reason,
                external_id=raw.external_id,
            )
            continue

        processed = await _process_one(
            session, portal=portal, normalized=normalized, moment=moment
        )
        result.processed.append(processed)

        if search_run_portal is not None:
            session.add(
                _raw_record(
                    raw,
                    portal=portal,
                    search_run_portal=search_run_portal,
                    status=(
                        RawJobStatus.DUPLICATE
                        if processed.verdict and processed.verdict.is_duplicate
                        else RawJobStatus.NORMALIZED
                    ),
                    job_id=processed.job_id,
                )
            )

    logger.info(
        "job_processing_completed",
        portal=portal.slug,
        collected=len(raw_jobs),
        jobs_created=result.jobs_created,
        sources_created=result.sources_created,
        duplicates=result.duplicates,
        rejected=len(result.rejected),
    )

    return result


async def _process_one(
    session: AsyncSession,
    *,
    portal: Portal,
    normalized: NormalizedJob,
    moment: datetime,
) -> ProcessedJob:
    """Resolve one normalised listing to a canonical job and a source row."""
    candidates = await _load_candidates(session, normalized)
    views = [
        view
        for job, sources in candidates
        if (view := _existing_view(job, sources)) is not None
    ]
    by_id = {job.id: (job, sources) for job, sources in candidates}

    match, verdict = best_match(normalized, views)

    created_job = False
    changed: tuple[str, ...] = ()

    if match is None:
        job = _new_job(normalized, moment=moment)
        session.add(job)
        # Flush so the job has an id for the source row's foreign key.
        await session.flush()
        sources: list[JobSource] = []
        created_job = True
    else:
        job, sources = by_id[match.job_id]  # type: ignore[index]
        outcome = merge_into_existing(
            existing_title=job.title,
            existing_company=job.company,
            existing_location=normalize_location(job.location),
            existing_work_mode=job.work_mode,
            existing_employment_type=job.employment_type,
            existing_description=job.description,
            existing_posted_at=job.posted_at,
            incoming=normalized,
        )
        _apply_changes(job, outcome.changed, incoming=normalized)
        changed = tuple(outcome.changed)
        job.last_seen_at = moment

    source, created_source = _upsert_source(
        session, job=job, portal=portal, normalized=normalized, moment=moment
    )
    if created_source:
        sources = [*sources, source]

    preferred_url, preferred_kind = _resolve_preferred_url(sources)
    job.preferred_application_url = preferred_url
    job.preferred_url_kind = preferred_kind

    # Flush before the next listing is considered. Duplicate lookup queries the
    # database, so a source that is still pending in the session would be
    # invisible - and the very next listing in the same batch could then create
    # a second canonical job for a vacancy this one already stored.
    await session.flush()

    return ProcessedJob(
        portal_slug=portal.slug,
        external_id=normalized.external_id,
        title=job.title,
        job_id=job.id,
        canonical_key=job.canonical_key,
        created_job=created_job,
        created_source=created_source,
        verdict=verdict if match is not None else None,
        changed_fields=changed,
        preferred_application_url=preferred_url,
        preferred_url_kind=preferred_kind,
    )


def _new_job(normalized: NormalizedJob, *, moment: datetime) -> Job:
    return Job(
        canonical_key=normalized.canonical_key,
        title=normalized.title.display,
        normalized_title=normalized.title.key,
        company=normalized.company.display if normalized.company else None,
        normalized_company=normalized.company.key if normalized.company else None,
        location=normalized.location.display,
        work_mode=normalized.work_mode,
        employment_type=normalized.employment_type,
        description=normalized.description,
        description_fingerprint=normalized.description_fingerprint,
        posted_at=normalized.posted_at,
        first_discovered_at=moment,
        last_seen_at=moment,
    )


def _apply_changes(
    job: Job, changed: dict[str, object], *, incoming: NormalizedJob
) -> None:
    """Write an agreed set of field improvements onto a stored job.

    The normalised comparison keys follow their display values, so a job whose
    title improved is still found by the same lookups next time.
    """
    for name, value in changed.items():
        setattr(job, name, value)

    if "title" in changed and job.title == incoming.title.display:
        job.normalized_title = incoming.title.key
    if "company" in changed and incoming.company is not None:
        job.normalized_company = incoming.company.key
    if "description" in changed:
        job.description_fingerprint = incoming.description_fingerprint


def _upsert_source(
    session: AsyncSession,
    *,
    job: Job,
    portal: Portal,
    normalized: NormalizedJob,
    moment: datetime,
) -> tuple[JobSource, bool]:
    """Add or refresh this portal's source row for a canonical job.

    One row per portal per job, so rediscovering a listing updates what is
    already there. The portal's own listing URL is stored exactly as
    normalisation produced it and is never replaced by another portal's.
    """
    existing: JobSource | None = None
    if _sources_loaded(job):
        existing = next(
            (source for source in job.sources if source.portal_id == portal.id), None
        )

    preferred = normalized.preferred_url

    if existing is not None:
        existing.external_id = normalized.external_id or existing.external_id
        existing.source_url = normalized.source_url.url
        existing.application_url = preferred.url
        existing.url_kind = preferred.kind
        existing.posted_at = normalized.posted_at or existing.posted_at
        existing.last_seen_at = moment
        return existing, False

    source = JobSource(
        job_id=job.id,
        portal_id=portal.id,
        external_id=normalized.external_id,
        source_url=normalized.source_url.url,
        application_url=preferred.url,
        url_kind=preferred.kind,
        posted_at=normalized.posted_at,
        first_seen_at=moment,
        last_seen_at=moment,
    )
    session.add(source)

    return source, True


def _sources_loaded(job: Job) -> bool:
    """True when ``job.sources`` can be read without emitting lazy SQL.

    The relationship is ``lazy="raise"``, so touching it on a freshly created
    job would raise rather than silently query.
    """
    return "sources" in job.__dict__


def _raw_record(
    raw: RawJob,
    *,
    portal: Portal,
    search_run_portal: SearchRunPortal,
    status: RawJobStatus,
    job_id: UUID | None = None,
    rejection_reason: str | None = None,
) -> RawJobRecord:
    """The capture exactly as the portal returned it, for the audit trail."""
    return RawJobRecord(
        search_run_portal_id=search_run_portal.id,
        portal_id=portal.id,
        job_id=job_id,
        external_id=raw.external_id,
        source=raw.source,
        title=raw.title,
        company=raw.company,
        location=raw.location,
        description=raw.description,
        source_url=raw.source_url,
        application_url=raw.application_url,
        posted_at=raw.posted_at,
        employment_type=raw.employment_type,
        raw_payload=raw.raw_payload or {},
        status=status,
        rejection_reason=rejection_reason,
    )


# ---------------------------------------------------------------------------
# Read-only preview
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PreviewJob:
    """What processing *would* produce for one listing. Nothing is stored."""

    portal_slug: str
    external_id: str | None
    title: str
    company: str | None
    location: str | None
    work_mode: str | None
    employment_type: str | None
    description_chars: int
    posted_at: datetime | None
    source_url: str
    application_url: str | None
    preferred_application_url: str
    preferred_url_kind: str
    canonical_key: str
    #: Index of the canonical group within this batch. Two listings sharing a
    #: group would become one job.
    group_index: int
    #: Set when this listing would attach to a job already in the database.
    existing_job_id: UUID | None = None
    duplicate_of_index: int | None = None
    verdict: dict[str, object] | None = None


@dataclass(slots=True)
class PreviewResult:
    """Outcome of a read-only processing preview."""

    portal_slug: str
    jobs: list[PreviewJob] = field(default_factory=list)
    rejected: list[RejectedJob] = field(default_factory=list)

    @property
    def canonical_groups(self) -> int:
        return len({job.group_index for job in self.jobs})


def _view_from_normalized(normalized: NormalizedJob, *, key: object) -> ExistingJobView:
    """Treat a not-yet-stored listing as a comparison candidate."""
    return ExistingJobView(
        job_id=key,
        canonical_key=normalized.canonical_key,
        title=normalized.title,
        company_key=normalized.company_key,
        company_display=normalized.company.display if normalized.company else None,
        location=normalized.location,
        posted_at=normalized.posted_at,
        description_fingerprint=normalized.description_fingerprint,
        source_canonical_urls=frozenset(normalized.candidate_canonical_urls),
        application_canonical_urls=frozenset(
            [normalized.application_url.canonical]
            if normalized.application_url is not None
            and normalized.application_url.kind
            in (JobUrlKind.ATS, JobUrlKind.EMPLOYER_CAREER)
            else []
        ),
        external_ids=frozenset(
            [(normalized.portal_slug, normalized.external_id)]
            if normalized.external_id
            else []
        ),
    )


async def preview_raw_jobs(
    session: AsyncSession,
    *,
    portal_slug: str,
    raw_jobs: list[RawJob],
    now: datetime | None = None,
) -> PreviewResult:
    """Show what processing would do, without writing anything.

    Runs the same normalisation and the same duplicate rules as
    ``process_raw_jobs`` and issues only SELECTs. Its purpose is to let a
    listing be inspected - normalised fields, chosen URL, duplicate verdict -
    before anything is committed.
    """
    result = PreviewResult(portal_slug=portal_slug)
    moment = now or datetime.now(UTC)

    # Representatives of the canonical groups found so far in this batch.
    batch_views: list[ExistingJobView] = []
    group_of: dict[int, int] = {}

    for index, raw in enumerate(raw_jobs):
        normalized = normalize_raw_job(raw, now=moment)
        if isinstance(normalized, RejectedJob):
            result.rejected.append(normalized)
            continue

        candidates = await _load_candidates(session, normalized)
        stored_views = [
            view
            for job, sources in candidates
            if (view := _existing_view(job, sources)) is not None
        ]
        stored_match, stored_verdict = best_match(normalized, stored_views)

        batch_match, batch_verdict = best_match(normalized, batch_views)

        if batch_match is not None:
            # Batch representatives carry their position in the batch as their
            # id, so the group this listing joins is looked up directly.
            duplicate_of = int(batch_match.job_id)  # type: ignore[call-overload]
            group_index = group_of[duplicate_of]
            verdict = batch_verdict
        else:
            group_index = len(batch_views)
            duplicate_of = None
            verdict = stored_verdict if stored_match is not None else None
            batch_views.append(_view_from_normalized(normalized, key=index))
            group_of[index] = group_index

        preferred = normalized.preferred_url
        result.jobs.append(
            PreviewJob(
                portal_slug=normalized.portal_slug,
                external_id=normalized.external_id,
                title=normalized.title.display,
                company=normalized.company.display if normalized.company else None,
                location=normalized.location.display,
                work_mode=normalized.work_mode.value if normalized.work_mode else None,
                employment_type=normalized.employment_type,
                description_chars=len(normalized.description or ""),
                posted_at=normalized.posted_at,
                source_url=normalized.source_url.url,
                application_url=(
                    normalized.application_url.url
                    if normalized.application_url
                    else None
                ),
                preferred_application_url=preferred.url,
                preferred_url_kind=preferred.kind.value,
                canonical_key=normalized.canonical_key,
                group_index=group_index,
                existing_job_id=(
                    stored_match.job_id if stored_match is not None else None  # type: ignore[arg-type]
                ),
                duplicate_of_index=duplicate_of,
                verdict=verdict.as_dict() if verdict is not None else None,
            )
        )

    return result


__all__ = [
    "MAX_DUPLICATE_CANDIDATES",
    "PreviewJob",
    "PreviewResult",
    "ProcessedJob",
    "ProcessingResult",
    "preview_raw_jobs",
    "process_raw_jobs",
]
