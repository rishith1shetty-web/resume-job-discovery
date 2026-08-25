"""Reading and writing the career-page validation cache.

Thin on purpose. The decisions live in ``app.careers.validation`` (what a page
is) and ``app.models.career_page_validation`` (how long a verdict is worth
reusing); this only moves rows.

# Failure is remembered briefly, never permanently

The brief is explicit that a cached failure must not eliminate a company for
good, and the shape of this module is what enforces it. Every row carries the
time it was written and nothing else - there is no "banned" flag, no attempt
counter, no escalating backoff. Freshness is recomputed from
``last_validated_at`` against a time-to-live chosen by outcome, so an
``UNAVAILABLE`` verdict stops applying half an hour later whether or not
anybody remembers to clear it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.careers.validation import ValidationResult
from app.core.logging import get_logger
from app.models.career_page_validation import CareerPageValidation

logger = get_logger(__name__)


async def load_fresh(
    session: AsyncSession,
    *,
    keys: Sequence[str],
    now: datetime | None = None,
) -> dict[str, CareerPageValidation]:
    """Cached verdicts that are still worth reusing, by career page key.

    One query for the whole run rather than one per company. Stale rows are
    filtered out here rather than in SQL because the time-to-live depends on
    the outcome, and expressing that as a CASE would put the policy in two
    places.
    """
    wanted = [key for key in dict.fromkeys(keys) if key]
    if not wanted:
        return {}

    moment = now or datetime.now(UTC)
    rows = (
        await session.scalars(
            select(CareerPageValidation).where(
                CareerPageValidation.career_page_key.in_(wanted)
            )
        )
    ).all()

    return {row.career_page_key: row for row in rows if row.is_fresh(now=moment)}


async def record(
    session: AsyncSession,
    *,
    career_page_key: str,
    company_name: str,
    candidate_url: str,
    result: ValidationResult,
) -> CareerPageValidation:
    """Store what validation concluded, replacing any earlier verdict.

    An upsert by hand rather than a dialect-specific ``ON CONFLICT``: the
    contention here is two concurrent runs reaching the same employer, which is
    rare and cheap to lose. A savepoint keeps a lost race from poisoning the
    surrounding transaction, which matters because that transaction is the
    user's whole search.
    """
    existing = await session.scalar(
        select(CareerPageValidation).where(
            CareerPageValidation.career_page_key == career_page_key
        )
    )

    if existing is None:
        row = CareerPageValidation(
            career_page_key=career_page_key,
            company_name=company_name[:256],
            candidate_url=candidate_url[:2048],
            status=result.status,
            validation_reason=result.reason,
            last_validated_at=result.checked_at,
            verified_url=result.url[:2048] if result.url else None,
            url_kind=result.kind.value if result.kind else None,
        )
        session.add(row)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            # Another run wrote this key first. Theirs is as good as ours.
            logger.debug("career_validation_race", key=career_page_key)
            existing = await session.scalar(
                select(CareerPageValidation).where(
                    CareerPageValidation.career_page_key == career_page_key
                )
            )
            if existing is None:
                raise
        else:
            return row

    existing.company_name = company_name[:256]
    existing.candidate_url = candidate_url[:2048]
    existing.status = result.status
    existing.validation_reason = result.reason
    existing.last_validated_at = result.checked_at
    existing.verified_url = result.url[:2048] if result.url else None
    existing.url_kind = result.kind.value if result.kind else None
    await session.flush()
    return existing


__all__ = ["load_fresh", "record"]
