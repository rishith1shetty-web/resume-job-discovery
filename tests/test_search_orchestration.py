"""The complete Find Matches workflow, against the database.

Every test runs inside a transaction that is rolled back, so nothing is
committed to resume_job_discovery. Records carry the pytest marker. Nothing
here truncates, drops or resets anything.

Portals are deterministic fakes (``tests/fake_portals.py``). Only one live
portal exists today, so multi-portal fairness cannot be demonstrated against
real portals - these tests verify the allocation rules, not any portal's
inventory.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.constants import (
    ApplicationStatus,
    FreshnessWindow,
    PortalRunStatus,
    SearchRunStatus,
    Seniority,
    SkillKind,
)
from app.models.application import Application
from app.models.job import Job, JobMatch, JobSource, RawJobRecord
from app.models.profile import ResumeSkill
from app.models.search import PortalError as PortalErrorRow
from app.models.search import SearchQuery
from app.portals.exceptions import PortalBlockedError, PortalParseError
from app.search.depth import ExhaustionReason, SearchDepth
from app.services import search_run_service
from app.services.search_orchestrator import execute_search_run
from tests.conftest import TEST_RECORD_MARKER
from tests.factories import make_job, make_portal, make_profile, make_resume, make_user
from tests.fake_portals import (
    ACCOUNTING_DESCRIPTION,
    FINANCE_DESCRIPTION,
    build_fake_portal,
    make_jobs,
    make_raw_job,
)

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)

RESUME_SKILLS = ("excel", "sql", "forecasting", "budgeting", "financial modeling")


async def analysed_resume(session: AsyncSession, *, user=None):
    """A Financial Analyst resume with enough profile to be scoreable."""
    user = user or await make_user(session)
    resume = await make_resume(session, user=user)
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
    return user, resume


async def build_run(
    session: AsyncSession,
    *,
    resume,
    specs: list[tuple[str, object]],
    limit: int = 20,
):
    """Create a run over fake portals and return it with its adapters.

    ``specs`` is a list of ``(slug, adapter_class)``. Adapters are passed to the
    orchestrator directly rather than registered globally, so tests cannot leak
    portals into each other.
    """
    portals = []
    adapters: dict[uuid.UUID, object] = {}

    for slug, adapter_class in specs:
        portal = await make_portal(
            session, slug=slug, adapter_key=adapter_class.slug, options={}
        )
        portals.append(portal)
        adapters[portal.id] = adapter_class()

    prepared = await search_run_service.create_search_run(
        session,
        resume=resume,
        portal_ids=[portal.id for portal in portals],
        requested_limit=limit,
        require_adapter=False,
    )
    return prepared, adapters


async def run_search(session: AsyncSession, prepared, adapters, *, now=None):
    return await execute_search_run(
        session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=now or NOW,
    )


# --- A successful single-portal run -----------------------------------------


async def test_a_single_portal_run_returns_qualified_jobs(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("solo", jobs=make_jobs("solo", 5, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("solo", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.status is SearchRunStatus.COMPLETED
    assert outcome.returned_count == 5
    assert all(item.final_score >= 70.0 for item in outcome.results)
    assert all(item.qualified for item in outcome.results)


async def test_the_run_records_its_lifecycle(db_session: AsyncSession) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("life", jobs=make_jobs("life", 3, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("life", adapter)]
    )

    await run_search(db_session, prepared, adapters)
    run = prepared.run

    assert run.status is SearchRunStatus.COMPLETED
    assert run.started_at is not None
    assert run.completed_at is not None
    assert run.started_at <= run.completed_at
    assert run.error_message is None
    assert prepared.legs[0].status is PortalRunStatus.COMPLETED


async def test_one_portal_may_fill_the_whole_request(
    db_session: AsyncSession,
) -> None:
    # The live reality today: Jobright is the only operational portal.
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("only", jobs=make_jobs("only", 25, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("only", adapter)], limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 20
    assert outcome.is_short is False


async def test_a_full_result_set_is_not_reported_as_short(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("full", jobs=make_jobs("full", 30, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("full", adapter)], limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 20
    assert not any("qualified at the locked" in w for w in outcome.warnings)


# --- Honest shortfalls ------------------------------------------------------


async def test_fewer_qualified_jobs_than_requested_returns_the_smaller_set(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("thin", jobs=make_jobs("thin", 3, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("thin", adapter)], limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 3
    assert outcome.is_short is True
    assert any("threshold is never lowered" in w for w in outcome.warnings)


async def test_jobs_below_the_threshold_are_never_returned(
    db_session: AsyncSession,
) -> None:
    # A different profession with heavily overlapping skills. High skill
    # overlap must never carry an incompatible role over the line.
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal(
        "wrong",
        jobs=make_jobs(
            "wrong", 6, title="Accountant", description=ACCOUNTING_DESCRIPTION, now=NOW
        ),
    )
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("wrong", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 0
    assert outcome.status is SearchRunStatus.COMPLETED
    assert prepared.legs[0].qualified_count == 0


async def test_the_threshold_stays_at_seventy_when_nothing_qualifies(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal(
        "gate",
        jobs=make_jobs("gate", 4, title="Data Engineer", now=NOW),
    )
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("gate", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.min_score == 70
    assert prepared.run.min_score == 70
    assert outcome.returned_count == 0

    # The jobs were scored and stored - they were rejected, not hidden.
    scored = await db_session.scalar(
        select(func.count())
        .select_from(JobMatch)
        .where(JobMatch.search_run_id == prepared.run.id)
    )
    assert scored == 4


async def test_an_empty_portal_completes_without_results(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("empty", jobs=[])
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("empty", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.status is SearchRunStatus.COMPLETED
    assert outcome.returned_count == 0
    assert prepared.legs[0].status is PortalRunStatus.COMPLETED
    assert prepared.legs[0].raw_count == 0


# --- Failure isolation ------------------------------------------------------


async def test_one_portal_failing_does_not_fail_the_run(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    good = build_fake_portal("good", jobs=make_jobs("good", 6, now=NOW))
    bad = build_fake_portal("bad", error=PortalParseError("shape", portal="bad"))

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("good", good), ("bad", bad)]
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.returned_count == 6
    assert [leg.status for leg in prepared.legs] == [
        PortalRunStatus.COMPLETED,
        PortalRunStatus.FAILED,
    ]
    assert any("failed" in warning for warning in outcome.warnings)


async def test_every_portal_failing_fails_the_run(db_session: AsyncSession) -> None:
    _, resume = await analysed_resume(db_session)
    first = build_fake_portal("f1", error=PortalParseError("a", portal="f1"))
    second = build_fake_portal(
        "f2", error=PortalBlockedError("access not authorized", portal="f2")
    )

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("f1", first), ("f2", second)]
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.status is SearchRunStatus.FAILED
    assert outcome.returned_count == 0
    assert prepared.run.error_message
    assert "access not authorized" in prepared.run.error_message


async def test_a_portal_failure_is_recorded_with_its_kind(
    db_session: AsyncSession,
) -> None:
    # A blocked portal must be reported as blocked
    # rather than as an empty result.
    _, resume = await analysed_resume(db_session)
    blocked = build_fake_portal(
        "blocked", error=PortalBlockedError("not authorized", portal="blocked")
    )
    good = build_fake_portal("ok", jobs=make_jobs("ok", 2, now=NOW))

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("blocked", blocked), ("ok", good)]
    )
    await run_search(db_session, prepared, adapters)

    errors = list(
        (
            await db_session.scalars(
                select(PortalErrorRow).where(
                    PortalErrorRow.search_run_id == prepared.run.id
                )
            )
        ).all()
    )

    assert len(errors) == 1
    assert errors[0].kind.value == "blocked"
    assert "not authorized" in errors[0].message
    assert prepared.legs[0].error


async def test_a_failed_leg_reports_no_results_it_did_not_keep(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    bad = build_fake_portal("rollback", error=RuntimeError("late failure"))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("rollback", bad)]
    )

    await run_search(db_session, prepared, adapters)

    assert prepared.legs[0].status is PortalRunStatus.FAILED
    assert prepared.legs[0].raw_count == 0
    assert prepared.legs[0].qualified_count == 0
    assert prepared.legs[0].selected_count == 0


# --- Fair allocation across portals -----------------------------------------


async def test_two_portals_are_not_given_a_share_each(
    db_session: AsyncSession,
) -> None:
    """Phase 12: the requested limit is no longer divided between portals.

    Both portals here hold equally good jobs, so the Top 20 is drawn from the
    combined pool of 40 rather than ten reserved slots each. What each portal
    contributes is an outcome of the ranking, not an input to it.
    """
    _, resume = await analysed_resume(db_session)
    left = build_fake_portal("left", jobs=make_jobs("left", 20, now=NOW))
    right = build_fake_portal("right", jobs=make_jobs("right", 20, now=NOW))

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("left", left), ("right", right)], limit=20
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 20
    # The pool held everything both portals qualified, not two capped shares.
    assert outcome.selection is not None
    assert outcome.selection.pool_size == 40
    assert sum(leg.selected_count for leg in prepared.legs) == 20
    # No leg was capped at limit / portal_count.
    assert [leg.target_quota for leg in prepared.legs] == [20, 20]


async def test_four_portals_contribute_by_rank_not_by_share(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    specs = [
        (f"q{index}", build_fake_portal(f"q{index}", jobs=make_jobs(f"q{index}", 20, now=NOW)))
        for index in range(4)
    ]

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=50
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 50
    assert outcome.selection is not None
    assert outcome.selection.pool_size == 80
    assert sum(leg.selected_count for leg in prepared.legs) == 50


async def test_one_strong_portal_may_supply_most_of_the_result(
    db_session: AsyncSession,
) -> None:
    """The change that matters: good jobs are no longer left behind.

    Under quotas the deep portal was held to its share while the thin portal's
    unfilled slots had to be redistributed. Now the deep portal simply
    contributes what ranks.
    """
    _, resume = await analysed_resume(db_session)
    thin = build_fake_portal("small", jobs=make_jobs("small", 2, now=NOW))
    deep = build_fake_portal("large", jobs=make_jobs("large", 40, now=NOW))

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("small", thin), ("large", deep)], limit=20
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 20
    assert outcome.selection is not None
    assert outcome.selection.pool_size == 42

    contributed = [leg.selected_count for leg in prepared.legs]
    assert sum(contributed) == 20
    # The deep portal is not capped at half the request.
    assert contributed[1] > 10


async def test_a_duplicate_job_does_not_consume_two_slots(
    db_session: AsyncSession,
) -> None:
    # The identical vacancy carried by two portals is one canonical job.
    shared = [
        make_raw_job(
            portal="alpha",
            index=99,
            company="Shared Employer",
            description=FINANCE_DESCRIPTION,
            now=NOW,
        )
    ]
    mirrored = [
        make_raw_job(
            portal="beta",
            index=99,
            company="Shared Employer",
            description=FINANCE_DESCRIPTION,
            now=NOW,
        )
    ]

    _, resume = await analysed_resume(db_session)
    first = build_fake_portal("alpha", jobs=shared)
    second = build_fake_portal("beta", jobs=mirrored)

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("alpha", first), ("beta", second)]
    )
    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 1
    assert len({item.job_id for item in outcome.results}) == 1

    # Both portals are still credited as sources - a duplicate is never dropped.
    sources = await db_session.scalar(
        select(func.count())
        .select_from(JobSource)
        .where(JobSource.job_id == outcome.results[0].job_id)
    )
    assert sources == 2
    assert len(outcome.results[0].portal_slugs) == 2


async def test_the_same_canonical_job_appears_once(db_session: AsyncSession) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("dupes", jobs=make_jobs("dupes", 8, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("dupes", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)
    ids = [item.job_id for item in outcome.results]

    assert len(ids) == len(set(ids))


# --- Resume-specific eligibility --------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        ApplicationStatus.APPLIED,
        ApplicationStatus.INTERVIEW,
        ApplicationStatus.REJECTED,
        ApplicationStatus.OFFER,
    ],
)
async def test_an_applied_job_is_excluded_for_that_resume(
    db_session: AsyncSession, status: ApplicationStatus
) -> None:
    user, resume = await analysed_resume(db_session)
    listing = make_raw_job(
        portal="hist", index=0, company="Known Employer", now=NOW
    )
    adapter = build_fake_portal("hist", jobs=[listing])

    # Discover it once so a canonical job exists to apply against.
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("hist", adapter)]
    )
    first = await run_search(db_session, prepared, adapters)
    assert first.returned_count == 1
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=status,
            applied_at=NOW,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    second_adapter = build_fake_portal("hist2", jobs=[listing])
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("hist2", second_adapter)]
    )
    second = await run_search(db_session, prepared2, adapters2)

    assert second.returned_count == 0
    assert prepared2.legs[0].status is PortalRunStatus.COMPLETED


async def test_a_skipped_job_is_excluded_for_that_resume(
    db_session: AsyncSession,
) -> None:
    """Skipped excludes like Applied, but records no submission.

    Deliberately written without ``applied_at``: skipping is a decision not to
    apply, so a run that came back with this job again would be offering back
    something the user already turned down.
    """
    user, resume = await analysed_resume(db_session)
    listing = make_raw_job(portal="skip", index=0, company="Passed Over", now=NOW)
    adapter = build_fake_portal("skip", jobs=[listing])

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("skip", adapter)]
    )
    first = await run_search(db_session, prepared, adapters)
    assert first.returned_count == 1
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=ApplicationStatus.SKIPPED,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    second_adapter = build_fake_portal("skip2", jobs=[listing])
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("skip2", second_adapter)]
    )
    second = await run_search(db_session, prepared2, adapters2)

    assert second.returned_count == 0
    assert prepared2.legs[0].status is PortalRunStatus.COMPLETED


@pytest.mark.parametrize(
    "status",
    [
        ApplicationStatus.NOT_APPLIED,
        ApplicationStatus.SAVED,
    ],
)
async def test_undecided_statuses_stay_eligible(
    db_session: AsyncSession, status: ApplicationStatus
) -> None:
    user, resume = await analysed_resume(db_session)
    listing = make_raw_job(portal="keep", index=0, company="Keep Co", now=NOW)
    adapter = build_fake_portal("keep", jobs=[listing])

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("keep", adapter)]
    )
    first = await run_search(db_session, prepared, adapters)
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=status,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    again = build_fake_portal("keep2", jobs=[listing])
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("keep2", again)]
    )
    second = await run_search(db_session, prepared2, adapters2)

    assert second.returned_count == 1
    assert second.results[0].job_id == job_id
    assert second.results[0].application_status is status


async def test_an_applied_job_stays_available_to_another_resume(
    db_session: AsyncSession,
) -> None:
    user, first_resume = await analysed_resume(db_session)
    _, second_resume = await analysed_resume(db_session, user=user)

    listing = make_raw_job(portal="two", index=0, company="Two Co", now=NOW)
    adapter = build_fake_portal("two", jobs=[listing])

    prepared, adapters = await build_run(
        db_session, resume=first_resume, specs=[("two", adapter)]
    )
    first = await run_search(db_session, prepared, adapters)
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=first_resume.id,
            job_id=job_id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    other = build_fake_portal("two2", jobs=[listing])
    prepared2, adapters2 = await build_run(
        db_session, resume=second_resume, specs=[("two2", other)]
    )
    second = await run_search(db_session, prepared2, adapters2)

    assert second.returned_count == 1
    assert second.results[0].job_id == job_id


async def test_another_portal_cannot_reintroduce_an_excluded_job(
    db_session: AsyncSession,
) -> None:
    # The exclusion is keyed on the canonical job, resolved after
    # deduplication, so a second portal's listing of the same vacancy is the
    # same row and stays excluded.
    user, resume = await analysed_resume(db_session)
    original = make_raw_job(
        portal="one", index=0, company="Mirror Co", description=FINANCE_DESCRIPTION, now=NOW
    )
    mirrored = make_raw_job(
        portal="two", index=0, company="Mirror Co", description=FINANCE_DESCRIPTION, now=NOW
    )

    first_adapter = build_fake_portal("one", jobs=[original])
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("one", first_adapter)]
    )
    first = await run_search(db_session, prepared, adapters)
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    second_adapter = build_fake_portal("two", jobs=[mirrored])
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("two", second_adapter)]
    )
    second = await run_search(db_session, prepared2, adapters2)

    assert second.returned_count == 0


async def test_an_excluded_job_does_not_consume_a_quota_slot(
    db_session: AsyncSession,
) -> None:
    user, resume = await analysed_resume(db_session)
    listings = make_jobs("quota", 5, now=NOW)
    adapter = build_fake_portal("quota", jobs=listings)

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("quota", adapter)]
    )
    first = await run_search(db_session, prepared, adapters)
    assert first.returned_count == 5

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=first.results[0].job_id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    again = build_fake_portal("quota2", jobs=listings)
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("quota2", again)]
    )
    second = await run_search(db_session, prepared2, adapters2)

    # Four remain, and the excluded one is counted as excluded rather than
    # silently occupying a slot.
    assert second.returned_count == 4
    assert second.portals[0].excluded_count == 1


async def test_an_applied_jobs_match_record_is_not_deleted(
    db_session: AsyncSession,
) -> None:
    user, resume = await analysed_resume(db_session)
    listing = make_raw_job(portal="hist", index=0, company="History Co", now=NOW)
    adapter = build_fake_portal("hist", jobs=[listing])

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("hist", adapter)]
    )
    first = await run_search(db_session, prepared, adapters)
    job_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=job_id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
            notes=TEST_RECORD_MARKER,
        )
    )
    await db_session.flush()

    again = build_fake_portal("hist2", jobs=[listing])
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("hist2", again)]
    )
    await run_search(db_session, prepared2, adapters2)

    surviving = await db_session.scalar(
        select(func.count())
        .select_from(JobMatch)
        .where(JobMatch.search_run_id == prepared.run.id, JobMatch.job_id == job_id)
    )
    assert surviving == 1


# --- Persistence and repeatability ------------------------------------------


async def test_every_executed_query_is_stored(db_session: AsyncSession) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("audit", jobs=make_jobs("audit", 30, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("audit", adapter)]
    )

    await run_search(db_session, prepared, adapters)

    rows = list(
        (
            await db_session.scalars(
                select(SearchQuery).where(
                    SearchQuery.search_run_portal_id == prepared.legs[0].id
                )
            )
        ).all()
    )

    assert rows
    assert all(row.query_text for row in rows)
    assert all(row.executed_at is not None for row in rows)


async def test_collected_listings_are_preserved_verbatim(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("raw", jobs=make_jobs("raw", 4, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("raw", adapter)]
    )

    await run_search(db_session, prepared, adapters)

    raw_rows = await db_session.scalar(
        select(func.count())
        .select_from(RawJobRecord)
        .where(RawJobRecord.search_run_portal_id == prepared.legs[0].id)
    )
    assert raw_rows == 4


async def test_per_portal_counters_are_recorded(db_session: AsyncSession) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("counts", jobs=make_jobs("counts", 6, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("counts", adapter)], limit=20
    )

    await run_search(db_session, prepared, adapters)
    leg = prepared.legs[0]

    assert leg.raw_count == 6
    assert leg.qualified_count == 6
    assert leg.selected_count == 6
    assert leg.target_quota == 20
    assert leg.deepest_window is not None


async def test_match_records_belong_to_their_run(db_session: AsyncSession) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("owned", jobs=make_jobs("owned", 3, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("owned", adapter)]
    )

    await run_search(db_session, prepared, adapters)

    matches = list(
        (
            await db_session.scalars(
                select(JobMatch).where(JobMatch.search_run_id == prepared.run.id)
            )
        ).all()
    )

    assert len(matches) == 3
    assert all(match.resume_id == resume.id for match in matches)
    assert all(match.ranking_score > 0 for match in matches)


async def test_the_run_summary_records_its_result_membership(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("member", jobs=make_jobs("member", 4, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("member", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)
    summary = dict(prepared.run.summary)

    assert summary["selected_job_ids"] == [
        str(item.job_id) for item in outcome.results
    ]
    assert summary["returned_count"] == outcome.returned_count
    assert summary["threshold"] == 70
    assert summary["queries"]


async def test_repeating_a_search_creates_no_duplicate_jobs_or_sources(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    listings = make_jobs("idem", 5, now=NOW)

    first_adapter = build_fake_portal("idem", jobs=listings)
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("idem", first_adapter)]
    )
    first = await run_search(db_session, prepared, adapters)

    jobs_after_first = await db_session.scalar(select(func.count()).select_from(Job))
    sources_after_first = await db_session.scalar(
        select(func.count()).select_from(JobSource)
    )

    # The same portal, searched again: same slug so the same source rows.
    second_adapter = build_fake_portal("idem", jobs=listings)
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("idem-again", second_adapter)]
    )
    second = await run_search(db_session, prepared2, adapters2)

    jobs_after_second = await db_session.scalar(select(func.count()).select_from(Job))

    assert jobs_after_second == jobs_after_first
    assert second.returned_count == first.returned_count
    assert {item.job_id for item in second.results} == {
        item.job_id for item in first.results
    }
    assert sources_after_first > 0


async def test_repeating_a_search_gives_the_same_order(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    listings = make_jobs("stable", 6, now=NOW)

    first_adapter = build_fake_portal("stable", jobs=listings)
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("stable", first_adapter)]
    )
    first = await run_search(db_session, prepared, adapters)

    second_adapter = build_fake_portal("stable", jobs=listings)
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("stable-2", second_adapter)]
    )
    second = await run_search(db_session, prepared2, adapters2)

    assert [item.job_id for item in first.results] == [
        item.job_id for item in second.results
    ]
    assert [item.final_score for item in first.results] == [
        item.final_score for item in second.results
    ]


async def test_a_new_run_preserves_the_previous_run_history(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    listings = make_jobs("history", 3, now=NOW)

    first_adapter = build_fake_portal("history", jobs=listings)
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("history", first_adapter)]
    )
    first = await run_search(db_session, prepared, adapters)
    first_summary = dict(prepared.run.summary)

    second_adapter = build_fake_portal("history2", jobs=listings)
    prepared2, adapters2 = await build_run(
        db_session, resume=resume, specs=[("history2", second_adapter)]
    )
    await run_search(db_session, prepared2, adapters2)

    # The earlier run keeps its own status, membership and match rows.
    assert prepared.run.status is SearchRunStatus.COMPLETED
    assert dict(prepared.run.summary) == first_summary

    old_matches = await db_session.scalar(
        select(func.count())
        .select_from(JobMatch)
        .where(JobMatch.search_run_id == prepared.run.id)
    )
    new_matches = await db_session.scalar(
        select(func.count())
        .select_from(JobMatch)
        .where(JobMatch.search_run_id == prepared2.run.id)
    )

    assert old_matches == first.returned_count
    assert new_matches == first.returned_count


async def test_results_can_be_read_back_in_the_returned_order(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("readback", jobs=make_jobs("readback", 5, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("readback", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)
    stored = await search_run_service.load_run_results(
        db_session, run=prepared.run
    )

    assert [item.job.id for item in stored] == [
        item.job_id for item in outcome.results
    ]
    assert all(item.match.qualified for item in stored)


# --- Freshness --------------------------------------------------------------


async def test_the_newest_qualified_jobs_come_first(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    listings = [
        make_raw_job(portal="fresh", index=0, age_days=20, now=NOW),
        make_raw_job(portal="fresh", index=1, age_days=0.2, now=NOW),
        make_raw_job(portal="fresh", index=2, age_days=6, now=NOW),
    ]
    adapter = build_fake_portal("fresh", jobs=listings)
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("fresh", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)
    scores = {item.final_score for item in outcome.results}

    # These listings differ only in age, so score is a tie and recency decides.
    assert len(scores) == 1
    ages = [item.posted_at for item in outcome.results]
    assert ages == sorted(ages, reverse=True)


async def test_a_listing_past_the_window_is_rejected_for_its_age(
    db_session: AsyncSession,
) -> None:
    # CLAUDE.md section 6: age alone IS a rejection reason. A 45-day-old
    # listing is excluded whatever it scores.
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal(
        "old", jobs=make_jobs("old", 3, age_days=45, now=NOW)
    )
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("old", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 0


async def test_a_listing_inside_the_window_is_still_returned(
    db_session: AsyncSession,
) -> None:
    """The other half of the rule: 28 days is eligible, 45 is not."""
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal(
        "recent", jobs=make_jobs("recent", 3, age_days=28, now=NOW)
    )
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("recent", adapter)]
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 3


async def test_collection_no_longer_stops_once_the_request_is_covered(
    db_session: AsyncSession,
) -> None:
    """Phase 12: a portal searches its whole ladder, not up to a quota.

    Under quotas this portal stopped at the newest window the moment it had 20
    qualified jobs, and everything older went uncollected. Now it walks the
    ladder to the end, so the ranking chooses from everything the portal has.
    """
    _, resume = await analysed_resume(db_session)
    adapter_class = build_fake_portal(
        "stop", jobs=make_jobs("stop", 40, age_days=0.2, now=NOW), date_filter=True
    )
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("stop", adapter_class)], limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)
    adapter = adapters[prepared.portals[0].id]

    windows = {call.window for call in adapter.calls}
    assert len(windows) > 1, "the ladder stopped early"
    assert FreshnessWindow.OLDER_ACTIVE in windows
    assert prepared.legs[0].deepest_window is FreshnessWindow.OLDER_ACTIVE

    # Ran out of listings rather than hitting a configured limit.
    assert outcome.portals[0].exhaustion is ExhaustionReason.EXHAUSTED
    assert outcome.returned_count == 20


# --- The deep unified engine, end to end (Phase 12) -------------------------
#
# Every test below drives the real orchestrator against fake portals. What they
# pin down is the change of shape: portals search independently and deeply,
# everything qualified lands in one pool, and the returned set is the Top N of
# that pool. Scoring, the threshold, the US filter, canonicalisation,
# deduplication and the ranking function itself are unchanged, and several of
# these assert exactly that.


async def test_the_run_reports_the_deep_unified_strategy(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("strat", jobs=make_jobs("strat", 6, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("strat", adapter)]
    )

    await run_search(db_session, prepared, adapters)
    summary = prepared.run.summary

    assert summary["search_strategy"] == "deep_unified"
    assert summary["candidate_pool_size"] == 6
    assert summary["portals_exhausted"] == 1
    assert summary["portals_limited"] == []
    assert summary["total_requests"] > 0


async def test_the_summary_reports_depth_per_portal(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("depth", jobs=make_jobs("depth", 8, now=NOW))
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("depth", adapter)]
    )

    await run_search(db_session, prepared, adapters)
    leg = prepared.run.summary["portals"][0]

    assert leg["exhausted"] is True
    assert leg["exhaustion"] == "exhausted"
    assert leg["requests"] > 0
    assert leg["pages"] >= 1
    assert leg["collection_seconds"] >= 0.0
    assert leg["collected"] == 8


async def test_the_candidate_pool_is_the_sum_of_every_portal(
    db_session: AsyncSession,
) -> None:
    """The example from the brief: nothing is discarded for another portal."""
    _, resume = await analysed_resume(db_session)
    specs = [
        ("a", build_fake_portal("a", jobs=make_jobs("a", 18, now=NOW))),
        ("b", build_fake_portal("b", jobs=make_jobs("b", 31, now=NOW))),
        ("c", build_fake_portal("c", jobs=make_jobs("c", 12, now=NOW))),
    ]
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.selection is not None
    assert outcome.selection.pool_size == 18 + 31 + 12
    assert outcome.returned_count == 20
    assert prepared.run.summary["candidate_pool_size"] == 61


async def test_every_portal_is_searched_to_its_own_depth(
    db_session: AsyncSession,
) -> None:
    """A portal is never cut short because another one had enough."""
    _, resume = await analysed_resume(db_session)
    deep = build_fake_portal("deep", jobs=make_jobs("deep", 40, now=NOW))
    small = build_fake_portal("small", jobs=make_jobs("small", 3, now=NOW))

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("deep", deep), ("small", small)], limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    # Twenty were requested; both portals still collected everything they had.
    assert [leg.collected_count for leg in outcome.portals] == [40, 3]
    assert outcome.selection is not None
    assert outcome.selection.pool_size == 43
    assert outcome.returned_count == 20


async def test_the_top_n_is_ranked_across_portals_not_within_them(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    specs = [
        ("x", build_fake_portal("x", jobs=make_jobs("x", 15, now=NOW))),
        ("y", build_fake_portal("y", jobs=make_jobs("y", 15, now=NOW))),
    ]
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    scores = [item.final_score for item in outcome.results]
    assert scores == sorted(scores, reverse=True)
    assert all(score >= 70.0 for score in scores)
    # Every returned job outranks every job left in the pool.
    returned = {item.job_id for item in outcome.results}
    left_behind = [
        candidate
        for leg in outcome.portals
        for candidate in leg.candidates
        if candidate.job_id not in returned
    ]
    if left_behind:
        assert min(scores) >= max(c.final_score for c in left_behind)


@pytest.mark.parametrize("limit", [20, 50, 100])
async def test_top_n_is_correct_for_each_supported_limit(
    db_session: AsyncSession, limit: int
) -> None:
    _, resume = await analysed_resume(db_session)
    specs = [
        (f"p{i}", build_fake_portal(f"p{i}", jobs=make_jobs(f"p{i}", 45, now=NOW)))
        for i in range(3)
    ]
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=limit
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.selection is not None
    assert outcome.selection.pool_size == 135
    assert outcome.returned_count == min(limit, 135)
    assert len({item.job_id for item in outcome.results}) == outcome.returned_count


# --- Deduplication after deep collection ------------------------------------


async def test_a_vacancy_on_two_portals_is_one_job_after_deep_collection(
    db_session: AsyncSession,
) -> None:
    """Deduplication is unchanged, and still runs on the deeper pool."""
    _, resume = await analysed_resume(db_session)
    shared = [
        make_raw_job(
            portal="alpha",
            index=99,
            company="Shared Employer",
            location="Austin, TX",
            description=FINANCE_DESCRIPTION,
            now=NOW,
        )
    ]
    mirrored = [
        make_raw_job(
            portal="beta",
            index=99,
            company="Shared Employer",
            location="Austin, TX",
            description=FINANCE_DESCRIPTION,
            now=NOW,
        )
    ]

    specs = [
        ("alpha", build_fake_portal("alpha", jobs=shared + make_jobs("alpha", 10, now=NOW))),
        ("beta", build_fake_portal("beta", jobs=mirrored + make_jobs("beta", 10, now=NOW))),
    ]
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=50
    )

    outcome = await run_search(db_session, prepared, adapters)

    # 22 collected, 21 distinct vacancies: the shared one merged.
    assert sum(leg.collected_count for leg in outcome.portals) == 22
    assert outcome.selection is not None
    assert outcome.selection.pool_size == 21

    job_ids = [item.job_id for item in outcome.results]
    assert len(job_ids) == len(set(job_ids))


async def test_a_duplicate_still_takes_one_place_in_the_top_n(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    listing = dict(
        company="One Vacancy",
        location="Boston, MA",
        description=FINANCE_DESCRIPTION,
        now=NOW,
    )
    specs = [
        ("one", build_fake_portal("one", jobs=[make_raw_job(portal="one", index=0, **listing)])),
        ("two", build_fake_portal("two", jobs=[make_raw_job(portal="two", index=0, **listing)])),
    ]
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.returned_count == 1
    sources = list(
        (
            await db_session.scalars(
                select(JobSource).where(JobSource.job_id == outcome.results[0].job_id)
            )
        ).all()
    )
    assert len(sources) == 2


# --- Guarantees that must survive the new engine ----------------------------


async def test_the_us_filter_still_runs_on_the_deeper_pool(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    foreign = [
        make_raw_job(
            portal="mixed",
            index=index,
            location="London, United Kingdom",
            description=FINANCE_DESCRIPTION,
            now=NOW,
        )
        for index in range(100, 106)
    ]
    adapter = build_fake_portal(
        "mixed", jobs=make_jobs("mixed", 6, now=NOW) + foreign
    )
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("mixed", adapter)], limit=50
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.portals[0].collected_count == 12
    assert outcome.portals[0].non_us_count == 6
    assert outcome.returned_count == 6
    assert prepared.run.summary["non_us_excluded_count"] == 6
    assert prepared.run.summary["location_policy"] == "united_states_only"


async def test_an_applied_job_is_still_excluded_from_the_pool(
    db_session: AsyncSession,
) -> None:
    user, resume = await analysed_resume(db_session)
    adapter = build_fake_portal("applied", jobs=make_jobs("applied", 8, now=NOW))

    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("applied", adapter)], limit=20
    )
    first = await run_search(db_session, prepared, adapters)
    applied_id = first.results[0].job_id

    db_session.add(
        Application(
            user_id=user.id,
            resume_id=resume.id,
            job_id=applied_id,
            status=ApplicationStatus.APPLIED,
            applied_at=NOW,
        )
    )
    await db_session.flush()

    again_prepared, again_adapters = await build_run(
        db_session, resume=resume, specs=[("applied2", adapter)], limit=20
    )
    second = await run_search(db_session, again_prepared, again_adapters)

    assert applied_id not in {item.job_id for item in second.results}
    assert second.returned_count == first.returned_count - 1


async def test_the_threshold_is_still_never_lowered_to_fill_the_request(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal(
        "weak",
        jobs=make_jobs(
            "weak", 30, title="Accountant", description=ACCOUNTING_DESCRIPTION, now=NOW
        ),
    )
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=[("weak", adapter)], limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.results == []
    assert outcome.selection is not None
    assert outcome.selection.pool_size == 0


# --- Failure isolation under concurrent collection --------------------------


async def test_one_portal_failing_does_not_stop_the_others(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    specs = [
        ("good", build_fake_portal("good", jobs=make_jobs("good", 10, now=NOW))),
        ("bad", build_fake_portal("bad", error=PortalParseError("down", portal="bad"))),
        ("also", build_fake_portal("also", jobs=make_jobs("also", 10, now=NOW))),
    ]
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.returned_count == 20
    statuses = [leg.status for leg in outcome.portals]
    assert statuses == [
        PortalRunStatus.COMPLETED,
        PortalRunStatus.FAILED,
        PortalRunStatus.COMPLETED,
    ]


async def test_several_portals_failing_still_returns_the_survivors(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    down = PortalBlockedError("refused", portal="x")
    specs = [
        ("f1", build_fake_portal("f1", error=down)),
        ("f2", build_fake_portal("f2", error=down)),
        ("alive", build_fake_portal("alive", jobs=make_jobs("alive", 12, now=NOW))),
    ]
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.status is SearchRunStatus.COMPLETED_WITH_ERRORS
    assert outcome.returned_count == 12
    assert sum(1 for leg in outcome.portals if leg.failed) == 2


async def test_a_failed_portal_contributes_nothing_to_the_pool(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    specs = [
        ("ok", build_fake_portal("ok", jobs=make_jobs("ok", 7, now=NOW))),
        ("dead", build_fake_portal("dead", error=PortalParseError("no", portal="dead"))),
    ]
    prepared, adapters = await build_run(
        db_session, resume=resume, specs=specs, limit=20
    )

    outcome = await run_search(db_session, prepared, adapters)

    assert outcome.selection is not None
    assert outcome.selection.pool_size == 7
    assert outcome.selection.per_portal_available == (7, 0)


# --- Determinism ------------------------------------------------------------


async def test_repeating_a_deep_search_returns_the_same_order_and_scores(
    db_session: AsyncSession,
) -> None:
    """Concurrent collection must not make the result order vary."""
    _, resume = await analysed_resume(db_session)

    def specs(tag: str):
        return [
            (f"a-{tag}", build_fake_portal(f"a{tag}", jobs=make_jobs("a", 20, now=NOW))),
            (f"b-{tag}", build_fake_portal(f"b{tag}", jobs=make_jobs("b", 20, now=NOW))),
            (f"c-{tag}", build_fake_portal(f"c{tag}", jobs=make_jobs("c", 20, now=NOW))),
        ]

    first_prepared, first_adapters = await build_run(
        db_session, resume=resume, specs=specs("one"), limit=50
    )
    first = await run_search(db_session, first_prepared, first_adapters)
    await db_session.commit()

    second_prepared, second_adapters = await build_run(
        db_session, resume=resume, specs=specs("two"), limit=50
    )
    second = await run_search(db_session, second_prepared, second_adapters)

    assert [i.job_id for i in first.results] == [i.job_id for i in second.results]
    assert [i.final_score for i in first.results] == [
        i.final_score for i in second.results
    ]


async def test_running_the_same_portals_concurrently_is_stable_across_runs(
    db_session: AsyncSession,
) -> None:
    """Five portals, run twice: attribution follows selection order, not luck."""
    _, resume = await analysed_resume(db_session)

    def specs(tag: str):
        return [
            (f"p{i}-{tag}", build_fake_portal(f"p{i}{tag}", jobs=make_jobs(f"p{i}", 12, now=NOW)))
            for i in range(5)
        ]

    first_prepared, first_adapters = await build_run(
        db_session, resume=resume, specs=specs("x"), limit=20
    )
    first = await run_search(db_session, first_prepared, first_adapters)
    first_split = [leg.selected_count for leg in first.portals]
    await db_session.commit()

    second_prepared, second_adapters = await build_run(
        db_session, resume=resume, specs=specs("y"), limit=20
    )
    second = await run_search(db_session, second_prepared, second_adapters)

    assert first_split == [leg.selected_count for leg in second.portals]
    assert [i.final_score for i in first.results] == [
        i.final_score for i in second.results
    ]


# --- Depth configuration reaches the portal ---------------------------------


async def test_a_configured_depth_limit_is_honoured_and_reported(
    db_session: AsyncSession,
) -> None:
    _, resume = await analysed_resume(db_session)
    adapter = build_fake_portal(
        "capped", jobs=make_jobs("capped", 40, now=NOW), date_filter=True
    )

    portal = await make_portal(
        db_session, slug="capped", adapter_key=adapter.slug, options={"max_jobs": 10}
    )
    prepared = await search_run_service.create_search_run(
        db_session,
        resume=resume,
        portal_ids=[portal.id],
        requested_limit=20,
        require_adapter=False,
    )
    outcome = await execute_search_run(
        db_session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters={portal.id: adapter()},
        now=NOW,
    )

    assert outcome.portals[0].collected_count == 10
    assert outcome.portals[0].exhaustion is ExhaustionReason.MAX_JOBS
    assert prepared.run.summary["portals_limited"] == ["max_jobs"]
    assert prepared.run.summary["portals_exhausted"] == 0
