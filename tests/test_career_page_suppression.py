"""A career page the user acted on does not come back on the next run.

The job lane has always excluded what the user already dealt with. These pin
the same guarantee for career pages **end to end through the real pipeline** -
a decision is recorded, ``execute_search_run`` is called again, and the page is
gone. Service-level tests live in ``test_career_page_interactions``; the point
of this file is that every layer between the two honours it, and that the
filtering is server-side rather than something a client is trusted to do.

The harness is imported from ``test_career_pages`` rather than rebuilt, so
these runs go through exactly the same fake portals and career source the rest
of the career-page suite uses.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ApplicationStatus
from app.services import career_page_service
from tests.fake_portals import build_fake_portal
from tests.test_career_pages import (
    analysed_resume,
    career_listings,
    prepare_run,
    run_search,
)


async def _decide(
    session: AsyncSession, *, user, resume, page, status: ApplicationStatus
):
    """Record a decision about one discovered career page."""
    identity = career_page_service.identify(
        official_career_url=page.official_career_url,
        company_name=page.company_name,
        relevant_role=page.relevant_role,
        location=page.location,
    )
    interaction, _ = await career_page_service.set_status(
        session,
        user_id=user.id,
        resume_id=resume.id,
        page=identity,
        status=status,
    )
    await session.flush()
    return interaction


def _companies(outcome) -> set[str]:
    return {page.company_name for page in outcome.career_pages}


async def _first_run(session: AsyncSession, *, resume, slug: str):
    prepared, adapters = await prepare_run(
        session,
        resume=resume,
        specs=[(slug, build_fake_portal(slug, jobs=career_listings()))],
        career=True,
    )
    return await run_search(session, prepared, adapters)


async def _next_run(session: AsyncSession, *, resume, slug: str):
    return await _first_run(session, resume=resume, slug=slug)


async def test_an_applied_career_page_is_gone_from_the_next_run(
    db_session: AsyncSession,
) -> None:
    user, resume = await analysed_resume(db_session)

    first = await _first_run(db_session, resume=resume, slug="appone")
    await db_session.commit()
    assert first.career_page_count >= 2, "need several pages to show selectivity"

    chosen = first.career_pages[0]
    await _decide(
        db_session, user=user, resume=resume, page=chosen,
        status=ApplicationStatus.APPLIED,
    )

    second = await _next_run(db_session, resume=resume, slug="apptwo")

    assert chosen.company_name not in _companies(second)
    # Only the one acted on went; every other page is still offered.
    assert _companies(second) == _companies(first) - {chosen.company_name}


async def test_a_skipped_career_page_is_gone_from_the_next_run(
    db_session: AsyncSession,
) -> None:
    user, resume = await analysed_resume(db_session)

    first = await _first_run(db_session, resume=resume, slug="skipone")
    await db_session.commit()
    assert first.career_page_count >= 2

    chosen = first.career_pages[0]
    await _decide(
        db_session, user=user, resume=resume, page=chosen,
        status=ApplicationStatus.SKIPPED,
    )

    second = await _next_run(db_session, resume=resume, slug="skiptwo")

    assert chosen.company_name not in _companies(second)


async def test_the_worked_example_from_the_brief(db_session: AsyncSession) -> None:
    """Apply to one, skip another; only the untouched one comes back."""
    user, resume = await analysed_resume(db_session)

    first = await _first_run(db_session, resume=resume, slug="wex1")
    await db_session.commit()
    assert first.career_page_count == 3

    applied, skipped, untouched = first.career_pages
    await _decide(
        db_session, user=user, resume=resume, page=applied,
        status=ApplicationStatus.APPLIED,
    )
    await _decide(
        db_session, user=user, resume=resume, page=skipped,
        status=ApplicationStatus.SKIPPED,
    )

    second = await _next_run(db_session, resume=resume, slug="wex2")

    assert _companies(second) == {untouched.company_name}


async def test_suppression_survives_a_third_run(db_session: AsyncSession) -> None:
    """The rule is stored, not a property of the run that recorded it."""
    user, resume = await analysed_resume(db_session)

    first = await _first_run(db_session, resume=resume, slug="thirda")
    await db_session.commit()
    chosen = first.career_pages[0]
    await _decide(
        db_session, user=user, resume=resume, page=chosen,
        status=ApplicationStatus.APPLIED,
    )

    await _next_run(db_session, resume=resume, slug="thirdb")
    third = await _next_run(db_session, resume=resume, slug="thirdc")

    assert chosen.company_name not in _companies(third)


async def test_the_run_says_how_many_career_pages_it_left_out(
    db_session: AsyncSession,
) -> None:
    """A shorter list is explained rather than silently returned."""
    user, resume = await analysed_resume(db_session)

    first = await _first_run(db_session, resume=resume, slug="warn1")
    await db_session.commit()
    await _decide(
        db_session, user=user, resume=resume, page=first.career_pages[0],
        status=ApplicationStatus.APPLIED,
    )

    second = await _next_run(db_session, resume=resume, slug="warn2")

    assert any(
        "already applied to or skipped" in warning for warning in second.warnings
    ), second.warnings


async def test_one_resumes_decision_leaves_another_resume_alone(
    db_session: AsyncSession,
) -> None:
    """Resume A applying to an employer must not hide it from Resume B."""
    user, resume_a = await analysed_resume(db_session)
    _, resume_b = await analysed_resume(db_session, user=user)

    first = await _first_run(db_session, resume=resume_a, slug="scope1")
    await db_session.commit()

    chosen = first.career_pages[0]
    await _decide(
        db_session, user=user, resume=resume_a, page=chosen,
        status=ApplicationStatus.APPLIED,
    )

    other = await _next_run(db_session, resume=resume_b, slug="scope2")

    assert chosen.company_name in _companies(other), (
        "Resume B never decided anything about this employer"
    )


async def test_a_career_page_decision_does_not_exclude_that_companys_jobs(
    db_session: AsyncSession,
) -> None:
    """Skipping the page is not skipping the vacancies behind it."""
    user, resume = await analysed_resume(db_session)

    first = await _first_run(db_session, resume=resume, slug="jobkeep1")
    await db_session.commit()
    assert first.returned_count > 0

    for page in first.career_pages:
        await _decide(
            db_session, user=user, resume=resume, page=page,
            status=ApplicationStatus.SKIPPED,
        )

    second = await _next_run(db_session, resume=resume, slug="jobkeep2")

    assert second.career_page_count == 0, "every page was skipped"
    assert second.returned_count == first.returned_count, (
        "the companies' individual jobs are untouched"
    )
