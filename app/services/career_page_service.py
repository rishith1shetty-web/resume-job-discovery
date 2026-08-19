"""Recording what a user decided about a company's career page.

The job equivalent is ``app.services.application_service`` and this mirrors its
shape: resolve the resume, upsert one row keyed by (user, resume, page), and
report whether anything actually changed so a repeated click is a no-op rather
than a duplicate.

Two things it deliberately does **not** do, because the product promises they
do not happen:

* It never writes an ``applications`` row. Applying to a career page is not
  applying to the vacancies listed on it.
* It never touches a job. Skipping an employer's page leaves every individual
  job from that employer exactly as it was.

Both are asserted by tests rather than left to this comment.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.careers.identity import career_page_key
from app.core.constants import ApplicationStatus
from app.core.exceptions import NotFoundError, UnprocessableError
from app.core.logging import get_logger
from app.models.career_page import (
    CAREER_PAGE_STATUSES,
    INELIGIBLE_CAREER_PAGE_STATUSES,
    CareerPageInteraction,
)
from app.models.resume import Resume

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CareerPageIdentity:
    """A career page reduced to what a decision needs to be stored against."""

    key: str
    company_name: str
    official_career_url: str
    relevant_role: str | None = None
    location: str | None = None


def identify(
    *,
    official_career_url: str | None,
    company_name: str | None,
    relevant_role: str | None = None,
    location: str | None = None,
) -> CareerPageIdentity:
    """Resolve a career page to its stable identity, or refuse it.

    Raises rather than inventing a key: a decision that cannot be matched back
    to the page on the next run would silently fail to suppress it, which looks
    exactly like the feature being broken.
    """
    key = career_page_key(
        official_career_url=official_career_url, company_name=company_name
    )
    if key is None:
        raise UnprocessableError(
            message=(
                "This career page cannot be identified: it has neither a "
                "usable career URL nor a company name."
            )
        )

    return CareerPageIdentity(
        key=key,
        company_name=(company_name or "").strip() or "Unknown company",
        official_career_url=(official_career_url or "").strip(),
        relevant_role=(relevant_role or None),
        location=(location or None),
    )


async def _require_resume(
    session: AsyncSession, *, resume_id: uuid.UUID, user_id: uuid.UUID
) -> Resume:
    resume = await session.get(Resume, resume_id)
    if resume is None or resume.user_id != user_id:
        raise NotFoundError(message=f"Resume {resume_id} was not found.")
    return resume


async def get_interaction(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    career_page_key: str,
) -> CareerPageInteraction | None:
    """This resume's decision about one career page, if it made one."""
    return await session.scalar(
        select(CareerPageInteraction).where(
            CareerPageInteraction.user_id == user_id,
            CareerPageInteraction.resume_id == resume_id,
            CareerPageInteraction.career_page_key == career_page_key,
        )
    )


async def set_status(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    page: CareerPageIdentity,
    status: ApplicationStatus,
    notes: str | None = None,
) -> tuple[CareerPageInteraction, bool]:
    """Record Applied or Skipped for one career page under one resume.

    Returns ``(interaction, changed)``. ``changed`` is False when the decision
    was already the requested one - a repeated click writes nothing and is not
    an error.

    Applied and Skipped are one column on one row, so setting either clears the
    other by construction; there is no window in which both hold.
    """
    if status not in CAREER_PAGE_STATUSES:
        raise UnprocessableError(
            message=(
                f"A career page can be marked "
                f"{' or '.join(sorted(s.value for s in CAREER_PAGE_STATUSES))}, "
                f"not {status.value!r}."
            )
        )

    await _require_resume(session, resume_id=resume_id, user_id=user_id)

    existing = await get_interaction(
        session, user_id=user_id, resume_id=resume_id, career_page_key=page.key
    )

    if existing is not None:
        changed = existing.status != status
        if notes is not None:
            existing.notes = notes
        # Refresh the snapshot: the employer may have moved their careers page
        # since the decision, and history should show where the user went.
        existing.company_name = page.company_name
        existing.official_career_url = page.official_career_url
        existing.relevant_role = page.relevant_role or existing.relevant_role
        existing.location = page.location or existing.location

        if changed:
            existing.status = status
            # The CHECK constraint ties applied_at to the status, so moving to
            # skipped must clear it rather than leave a submission time behind.
            existing.applied_at = (
                datetime.now(UTC) if status is ApplicationStatus.APPLIED else None
            )
            logger.info(
                "career_page_status_changed",
                resume_id=str(resume_id),
                career_page_key=page.key,
                new_status=status.value,
            )
        else:
            logger.info(
                "career_page_status_unchanged",
                resume_id=str(resume_id),
                career_page_key=page.key,
                status=status.value,
            )

        await session.flush()
        return existing, changed

    interaction = CareerPageInteraction(
        user_id=user_id,
        resume_id=resume_id,
        career_page_key=page.key,
        company_name=page.company_name,
        official_career_url=page.official_career_url,
        relevant_role=page.relevant_role,
        location=page.location,
        status=status,
        applied_at=(
            datetime.now(UTC) if status is ApplicationStatus.APPLIED else None
        ),
        notes=notes,
    )
    # A savepoint, not the outer transaction: two clicks racing on the same
    # page must cost the losing insert and nothing else. Rolling the whole
    # transaction back would discard unrelated work in the same request.
    savepoint = await session.begin_nested()
    session.add(interaction)

    try:
        await session.flush()
    except IntegrityError:
        await savepoint.rollback()
        # The unique constraint is what makes the second click safe; adopt the
        # row that won rather than fail the request the user just made.
        winner = await get_interaction(
            session, user_id=user_id, resume_id=resume_id, career_page_key=page.key
        )
        if winner is None:  # pragma: no cover - defensive
            raise
        logger.info(
            "career_page_interaction_adopted_concurrent_row",
            resume_id=str(resume_id),
            career_page_key=page.key,
        )
        return winner, winner.status != status

    await savepoint.commit()

    logger.info(
        "career_page_interaction_created",
        resume_id=str(resume_id),
        career_page_key=page.key,
        status=status.value,
    )
    return interaction, True


async def clear_status(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID,
    career_page_key: str,
) -> bool:
    """Undo a decision, making the page eligible again for this resume.

    Absence of a row *is* "no decision", so undoing means deleting rather than
    storing a third state. Returns whether anything was removed.
    """
    await _require_resume(session, resume_id=resume_id, user_id=user_id)

    existing = await get_interaction(
        session, user_id=user_id, resume_id=resume_id, career_page_key=career_page_key
    )
    if existing is None:
        return False

    await session.delete(existing)
    await session.flush()
    logger.info(
        "career_page_interaction_cleared",
        resume_id=str(resume_id),
        career_page_key=career_page_key,
    )
    return True


async def ineligible_career_page_keys(
    session: AsyncSession, *, resume_id: uuid.UUID
) -> frozenset[str]:
    """Career page keys this resume must not be offered again.

    The career-lane counterpart of ``app.matching.eligibility.ineligible_job_ids``
    and scoped the same way: to one resume, because a decision belongs to the
    resume it was made with (CLAUDE.md section 11). Resume A applying to an
    employer leaves that employer's page available to Resume B.
    """
    rows = await session.scalars(
        select(CareerPageInteraction.career_page_key).where(
            CareerPageInteraction.resume_id == resume_id,
            CareerPageInteraction.status.in_(
                tuple(INELIGIBLE_CAREER_PAGE_STATUSES)
            ),
        )
    )
    return frozenset(rows.all())


async def list_interactions(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    resume_id: uuid.UUID | None = None,
    status: ApplicationStatus | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[CareerPageInteraction], int]:
    """Decisions for the history view, most recently changed first."""
    filters = [CareerPageInteraction.user_id == user_id]
    if resume_id is not None:
        filters.append(CareerPageInteraction.resume_id == resume_id)
    if status is not None:
        filters.append(CareerPageInteraction.status == status)

    total = await session.scalar(
        select(func.count()).select_from(CareerPageInteraction).where(*filters)
    )

    rows = await session.scalars(
        select(CareerPageInteraction)
        .where(*filters)
        .order_by(CareerPageInteraction.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return list(rows.all()), int(total or 0)


__all__ = [
    "CareerPageIdentity",
    "clear_status",
    "get_interaction",
    "identify",
    "ineligible_career_page_keys",
    "list_interactions",
    "set_status",
]
