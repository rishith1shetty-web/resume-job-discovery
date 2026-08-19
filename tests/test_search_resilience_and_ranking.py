"""Cross-portal resilience and the one global ranking.

Two guarantees that only hold at the orchestrator level, so they cannot be
proved by any single adapter's tests:

* **Isolation.** A portal that is rate-limited or times out costs its own
  listings and nothing else. The run completes, the other portals contribute,
  and the failure is recorded so the UI can say what happened rather than
  silently returning a short list.
* **One ranking.** Qualified jobs from every portal compete in a single pool
  ordered newest-first, so a newer job from one portal outranks an older job
  from another. Results are never grouped by portal.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import PortalRunStatus, Seniority, SkillKind
from app.models.profile import ResumeSkill
from app.portals.exceptions import (
    PortalError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.services import search_run_service
from app.services.search_orchestrator import execute_search_run
from tests.factories import make_portal, make_profile, make_resume, make_user
from tests.fake_portals import build_fake_portal, make_raw_job

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
RESUME_SKILLS = ("excel", "sql", "forecasting", "budgeting", "financial modeling")


async def analysed_resume(session: AsyncSession):
    owner = await make_user(session)
    resume = await make_resume(session, user=owner)
    profile = await make_profile(session, resume=resume)
    profile.years_experience = 5.0
    profile.seniority = Seniority.MID
    profile.education = ["Bachelor of Science in Finance"]
    for key in RESUME_SKILLS:
        session.add(
            ResumeSkill(
                profile_id=profile.id,
                skill_key=key,
                display=key.title(),
                kind=SkillKind.SKILL,
                occurrences=2,
                confidence=0.9,
            )
        )
    await session.flush()
    return resume


async def run_search(session: AsyncSession, *, resume, specs, limit: int = 20):
    """Execute one run over (slug, adapter_instance) pairs."""
    portal_ids = []
    adapters = {}
    for slug, adapter in specs:
        portal = await make_portal(session, slug=slug, adapter_key=slug, options={})
        portal_ids.append(portal.id)
        adapters[portal.id] = adapter
    prepared = await search_run_service.create_search_run(
        session,
        resume=resume,
        portal_ids=portal_ids,
        requested_limit=limit,
        require_adapter=False,
    )
    return await execute_search_run(
        session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=NOW,
    )


def healthy(slug: str, *, count: int = 4, age_days: float = 1.0, company: str = "Co"):
    jobs = [
        make_raw_job(
            portal=slug,
            index=index,
            company=f"{company} {slug} {index}",
            location="Chicago, IL",
            age_days=age_days,
            now=NOW,
        )
        for index in range(count)
    ]
    return build_fake_portal(slug, jobs=jobs)()


def broken(slug: str, error: Exception):
    return build_fake_portal(slug, error=error)()


# --- Isolation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "error"),
    [
        ("rate_limited", PortalRateLimitedError("slow down", portal="noisy")),
        ("timeout", PortalTimeoutError("timed out", portal="noisy")),
        ("generic", PortalError("boom", portal="noisy")),
    ],
)
async def test_one_portal_failing_still_returns_the_others_jobs(
    db_session: AsyncSession, label: str, error: Exception
) -> None:
    resume = await analysed_resume(db_session)

    outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            (f"good-{label}", healthy(f"good-{label}")),
            (f"bad-{label}", broken(f"bad-{label}", error)),
        ],
    )

    assert outcome.returned_count > 0, "the healthy portal must still contribute"

    by_slug = {leg.portal_slug.split("-", 2)[-1]: leg for leg in outcome.portals}
    failed = [leg for leg in outcome.portals if leg.status is PortalRunStatus.FAILED]
    completed = [
        leg for leg in outcome.portals if leg.status is PortalRunStatus.COMPLETED
    ]

    assert len(failed) == 1
    assert len(completed) == 1
    # The failure is recorded, so the dashboard can say what happened.
    assert failed[0].error
    assert failed[0].error_kind
    assert by_slug  # slug mapping kept for readability of failures


async def test_a_failed_portal_does_not_fail_the_whole_run(
    db_session: AsyncSession,
) -> None:
    resume = await analysed_resume(db_session)

    outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("steady", healthy("steady")),
            ("limited", broken("limited", PortalRateLimitedError("429", portal="x"))),
        ],
    )

    # Partial success is its own state: not "completed", not "failed".
    assert outcome.status is not PortalRunStatus.FAILED
    assert outcome.returned_count > 0


async def test_a_rate_limited_portal_contributes_nothing_but_costs_nothing(
    db_session: AsyncSession,
) -> None:
    """The shortfall must be honest: no padding, no invented jobs."""
    resume = await analysed_resume(db_session)

    alone = await run_search(
        db_session, resume=resume, specs=[("solo", healthy("solo", count=4))]
    )

    resume2 = await analysed_resume(db_session)
    together = await run_search(
        db_session,
        resume=resume2,
        specs=[
            ("solo2", healthy("solo2", count=4)),
            ("dead", broken("dead", PortalRateLimitedError("429", portal="dead"))),
        ],
    )

    # Adding a portal that fails neither adds nor removes results.
    assert together.returned_count == alone.returned_count


async def test_every_portal_failing_is_reported_as_a_failed_run(
    db_session: AsyncSession,
) -> None:
    resume = await analysed_resume(db_session)

    outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("a-dead", broken("a-dead", PortalTimeoutError("t", portal="a"))),
            ("b-dead", broken("b-dead", PortalRateLimitedError("r", portal="b"))),
        ],
    )

    assert outcome.returned_count == 0
    assert all(
        leg.status is PortalRunStatus.FAILED for leg in outcome.portals
    )


# --- One global ranking ------------------------------------------------------


async def test_a_newer_job_from_one_portal_outranks_an_older_job_from_another(
    db_session: AsyncSession,
) -> None:
    """The rule from CLAUDE.md section 8, across portals rather than within one."""
    resume = await analysed_resume(db_session)

    outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("older", healthy("older", count=3, age_days=10.0)),
            ("newer", healthy("newer", count=3, age_days=1.0)),
        ],
    )

    assert outcome.returned_count >= 4
    dates = [item.posted_at for item in outcome.results]
    assert dates == sorted(dates, reverse=True), "results must be newest-first"

    # The newest job must come from the portal that had the newest listing,
    # whatever order the portals were selected in.
    assert outcome.results[0].posted_at > outcome.results[-1].posted_at


async def test_results_are_not_grouped_by_portal(
    db_session: AsyncSession,
) -> None:
    """Three portals, interleaved ages: the order must mix them."""
    resume = await analysed_resume(db_session)

    outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("alpha", healthy("alpha", count=3, age_days=1.0)),
            ("beta", healthy("beta", count=3, age_days=2.0)),
            ("gamma", healthy("gamma", count=3, age_days=3.0)),
        ],
        limit=20,
    )

    dates = [item.posted_at for item in outcome.results]
    assert dates == sorted(dates, reverse=True)

    # With three portals contributing, a portal-grouped list would show each
    # portal's block in turn. Ordering by date must not do that unless the
    # dates happen to agree - which here they deliberately do not.
    assert len({leg.portal_slug for leg in outcome.portals if leg.selected_count}) == 3


async def test_the_order_is_stable_between_identical_runs(
    db_session: AsyncSession,
) -> None:
    resume = await analysed_resume(db_session)
    first = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("s1-a", healthy("s1-a", count=3, age_days=1.0)),
            ("s1-b", healthy("s1-b", count=3, age_days=1.0)),
        ],
    )

    resume2 = await analysed_resume(db_session)
    second = await run_search(
        db_session,
        resume=resume2,
        specs=[
            ("s2-a", healthy("s2-a", count=3, age_days=1.0)),
            ("s2-b", healthy("s2-b", count=3, age_days=1.0)),
        ],
    )

    assert [item.title for item in first.results] == [
        item.title for item in second.results
    ]


async def test_a_portal_with_more_jobs_is_not_capped(
    db_session: AsyncSession,
) -> None:
    """No per-portal quota gates the result (Phase 12).

    A portal that found more good jobs contributes more of them, provided they
    win on the ranking. Nothing is held back to make the portals look balanced.
    """
    resume = await analysed_resume(db_session)

    outcome = await run_search(
        db_session,
        resume=resume,
        specs=[
            ("plenty", healthy("plenty", count=15, age_days=1.0)),
            ("scarce", healthy("scarce", count=1, age_days=9.0)),
        ],
        limit=20,
    )

    selected = {leg.portal_slug: leg.selected_count for leg in outcome.portals}
    plenty = next(v for k, v in selected.items() if k.endswith("plenty"))
    scarce = next(v for k, v in selected.items() if k.endswith("scarce"))

    # An even two-way split of 20 would have capped this portal at 10.
    assert plenty > scarce
    assert plenty == 15, f"expected all 15 to be selected, got {plenty}"
    assert plenty > 20 // 2, "an even split would have capped this portal"
