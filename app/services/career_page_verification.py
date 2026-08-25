"""Turning discovered companies into verified career pages, by fetching them.

Discovery says *which employers are hiring for this resume's role in the US*.
This says *which of them publish a career page a person can actually use*, and
it answers by opening the page.

# Why the cap is applied here and not in discovery

A run wants up to twenty companies. Roughly half of all candidates fail
validation - measured on live employer sites, 24 of 45 verified - so cutting
the pool to twenty before fetching would reliably return about eleven. The
pool therefore arrives whole, ranked by relevance, and is validated from the
top until twenty companies have *proved* themselves or the evidence runs out.

That is also why the ceiling is never met by lowering the bar. Twenty is a
maximum, not a target: eleven verified companies are returned as eleven.

# Cost control

Validation is the only part of this project that opens connections to
arbitrary employers' sites, so it is bounded on every axis: a shared fetcher
with a per-run response cache, a concurrency limit, a cached verdict per
employer with a time-to-live, and early exit the moment the ceiling is
reached. A company past the ceiling is never fetched at all.

# One company, one card

Deduplication happened in discovery, on normalised company identity. Nothing
here can undo it: the pool holds one entry per company, so twenty verified
results are twenty different employers by construction rather than by a
filtering step that could be forgotten.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.careers.base import CareerPage, CareerPageDiscovery
from app.careers.fetching import CareerPageFetcher
from app.careers.identity import career_page_key
from app.careers.validation import ValidationResult, validate_company
from app.careers.verification import CandidateCareerUrl, admit_career_url
from app.core.constants import CareerPageStatus, JobUrlKind
from app.core.logging import get_logger
from app.models.career_page_validation import CareerPageValidation
from app.services import career_page_validation_service

logger = get_logger(__name__)

#: Employers checked at once. Enough that twenty companies resolve in seconds,
#: low enough that this never looks like a burst of automated traffic - each
#: connection goes to a *different* employer, so no single site sees more than
#: its own page being requested.
DEFAULT_CONCURRENCY = 6

#: Companies validated in one run, however many were discovered. A ceiling on
#: work, not on quality: it is comfortably above the twenty cards a run can
#: return and the roughly half of candidates that fail, so it only bites when
#: a search turns up an unusually large pool.
MAX_COMPANIES_VALIDATED = 60


def _cache_key(page: CareerPage) -> str | None:
    """The cache identity for a discovered company.

    Derived from the strongest candidate's host plus the employer, which is the
    same construction ``career_page_key`` uses for a verified page. A company
    whose candidate set changes between runs simply misses the cache and is
    re-fetched - the cost of a miss is one request, so it is not worth a
    cleverer key.
    """
    if not page.candidate_urls:
        return None
    return career_page_key(
        official_career_url=page.candidate_urls[0],
        company_name=page.company_name,
    )


def _candidates_of(page: CareerPage) -> list[CandidateCareerUrl]:
    """Re-admit the stored candidate URLs as typed candidates.

    ``CareerPage`` carries plain strings so it stays a value that can cross a
    serialisation boundary. The kinds are recomputed rather than stored,
    because ``admit_career_url`` is the one place allowed to decide that a URL
    could be an employer's - duplicating its verdict on the page would let the
    two drift.
    """
    candidates: list[CandidateCareerUrl] = []
    for url in page.candidate_urls:
        admitted = admit_career_url(url, company=page.company_name)
        if admitted is not None:
            candidates.append(admitted)
    return candidates


def _verified(page: CareerPage, result: ValidationResult) -> CareerPage:
    return replace(
        page,
        status=CareerPageStatus.VERIFIED,
        official_career_url=result.url or "",
        url_kind=result.kind or page.url_kind,
        validation_reason=result.reason,
        last_validated_at=result.checked_at,
    )


def _refused(page: CareerPage, result: ValidationResult) -> CareerPage:
    return replace(
        page,
        status=result.status,
        # Emphatically cleared. A page that did not verify must carry no URL at
        # all, so that no serialiser, template or future caller can offer one:
        # there is nothing there to render as a link.
        official_career_url="",
        validation_reason=result.reason,
        last_validated_at=result.checked_at,
    )


def _from_cache(page: CareerPage, row: CareerPageValidation) -> CareerPage:
    """Apply a cached verdict without fetching anything."""
    if row.status is not CareerPageStatus.VERIFIED or not row.verified_url:
        return replace(
            page,
            status=row.status,
            official_career_url="",
            validation_reason=row.validation_reason or "",
            last_validated_at=row.last_validated_at,
        )

    try:
        kind = JobUrlKind(row.url_kind) if row.url_kind else page.url_kind
    except ValueError:
        kind = page.url_kind

    return replace(
        page,
        status=CareerPageStatus.VERIFIED,
        official_career_url=row.verified_url,
        url_kind=kind,
        validation_reason=row.validation_reason or "",
        last_validated_at=row.last_validated_at,
    )


async def verify_career_pages(
    session: AsyncSession,
    discovery: CareerPageDiscovery,
    *,
    ceiling: int | None = None,
    fetcher: CareerPageFetcher | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    now: datetime | None = None,
) -> list[CareerPage]:
    """Fetch and check discovered companies, returning only usable ones.

    Returns up to ``ceiling`` verified career pages, ranked as discovery ranked
    them. Companies that could not be verified are not returned at all - the
    caller learns how many there were from the discovery counters, not from a
    half-populated card.

    ``fetcher`` is injectable so tests can drive the whole path over mocked
    HTTP without a network.
    """
    moment = now or datetime.now(UTC)
    limit = discovery.candidate_ceiling if ceiling is None else ceiling
    pool = discovery.pages[:MAX_COMPANIES_VALIDATED]

    if limit <= 0 or not pool:
        return []

    keys = {id(page): _cache_key(page) for page in pool}
    cached = await career_page_validation_service.load_fresh(
        session,
        keys=[key for key in keys.values() if key],
        now=moment,
    )

    verified: list[CareerPage] = []
    rejected = 0
    checked_urls = 0
    from_cache = 0

    async def resolve(
        page: CareerPage, owned: CareerPageFetcher
    ) -> tuple[CareerPage, ValidationResult | None, int]:
        candidates = _candidates_of(page)
        if not candidates:
            return (
                replace(
                    page,
                    status=CareerPageStatus.UNVERIFIED,
                    official_career_url="",
                    validation_reason=(
                        "No career page URL was published with this company's "
                        "postings."
                    ),
                    last_validated_at=moment,
                ),
                None,
                0,
            )

        outcome = await validate_company(
            candidates, company=page.company_name, fetcher=owned, now=moment
        )
        settled = (
            _verified(page, outcome.result)
            if outcome.result.verified
            else _refused(page, outcome.result)
        )
        return settled, outcome.result, len(outcome.attempted)

    async def run(owned: CareerPageFetcher) -> None:
        nonlocal rejected, checked_urls, from_cache

        pending = list(pool)
        index = 0
        while index < len(pending) and len(verified) < limit:
            # A wave at a time, so the ceiling can stop the next one. Anything
            # past it is never requested, which is the whole cost control.
            wave = pending[index : index + concurrency]
            index += len(wave)

            live: list[CareerPage] = []
            for page in wave:
                key = keys.get(id(page))
                row = cached.get(key) if key else None
                if row is not None:
                    from_cache += 1
                    settled = _from_cache(page, row)
                    if settled.is_verified:
                        verified.append(settled)
                    else:
                        rejected += 1
                    continue
                live.append(page)

            if not live:
                continue

            results = await asyncio.gather(
                *(resolve(page, owned) for page in live)
            )

            for original, (settled, result, attempts) in zip(
                live, results, strict=True
            ):
                checked_urls += attempts
                if settled.is_verified:
                    verified.append(settled)
                else:
                    rejected += 1

                key = keys.get(id(original))
                if key and result is not None and attempts:
                    await career_page_validation_service.record(
                        session,
                        career_page_key=key,
                        company_name=original.company_name,
                        candidate_url=original.candidate_urls[0],
                        result=result,
                    )

    if fetcher is not None:
        await run(fetcher)
    else:
        async with CareerPageFetcher() as owned:
            await run(owned)

    discovery.rejected_companies = rejected
    discovery.urls_checked = checked_urls

    logger.info(
        "career_pages_verified",
        pool=len(pool),
        verified=len(verified),
        rejected=rejected,
        urls_checked=checked_urls,
        from_cache=from_cache,
        ceiling=limit,
    )

    return verified[:limit]


__all__ = [
    "DEFAULT_CONCURRENCY",
    "MAX_COMPANIES_VALIDATED",
    "verify_career_pages",
]
