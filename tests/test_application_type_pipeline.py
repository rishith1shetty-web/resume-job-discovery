"""The application-type filter inside the full search pipeline (Phase 13).

The classifier is tested in ``test_application_type.py``. What is tested here
is where it sits: a Quick Apply listing must be gone before scoring, before
canonicalisation and before deduplication, it must be counted honestly in the
run summary, and everything that used to qualify must still qualify.

Every test runs inside a transaction that is rolled back, so nothing is
committed to resume_job_discovery. Portals are deterministic fakes; nothing
here makes a network request.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ApplicationType, RawJobStatus, SearchRunStatus
from app.models.job import Job, JobMatch, RawJobRecord
from app.portals.base import declare_application_type
from app.processing.application_type import QUICK_APPLY_REJECTION
from app.processing.normalize import RejectionReason
from app.services import search_run_service
from app.services.job_processing import process_raw_jobs
from app.services.search_orchestrator import (
    LONG_APPLICATION_POLICY,
    execute_search_run,
)
from tests.factories import make_portal
from tests.fake_portals import build_fake_portal, make_jobs, make_raw_job
from tests.test_search_orchestration import NOW, analysed_resume


def quick_apply(job):
    """Mark one fake listing the way a portal would declare Quick Apply."""
    declare_application_type(job.raw_payload, ApplicationType.QUICK_APPLY)
    return job


def long_application(job):
    declare_application_type(job.raw_payload, ApplicationType.LONG_APPLICATION)
    return job


async def run_over(
    session: AsyncSession, *, resume, specs, limit: int = 20
):
    """Create and execute a run over the given ``(slug, adapter)`` fakes."""
    portals, adapters = [], {}
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
    outcome = await execute_search_run(
        session,
        run=prepared.run,
        legs=prepared.legs,
        portals=prepared.portals,
        adapters=adapters,
        now=NOW,
    )
    return prepared, outcome


# ---------------------------------------------------------------------------
# Processing: rejected before anything is written
# ---------------------------------------------------------------------------


class TestProcessingRejectsQuickApply:
    async def test_a_quick_apply_listing_creates_no_job(
        self, db_session: AsyncSession
    ) -> None:
        portal = await make_portal(db_session, slug="qa", options={})
        before = await db_session.scalar(select(func.count()).select_from(Job))

        result = await process_raw_jobs(
            db_session,
            portal=portal,
            raw_jobs=[quick_apply(make_raw_job(portal="qa", index=1, now=NOW))],
        )

        assert result.processed == []
        assert len(result.rejected) == 1
        assert result.rejected[0].reason == QUICK_APPLY_REJECTION
        after = await db_session.scalar(select(func.count()).select_from(Job))
        assert after == before, "a refused listing was still stored as a job"

    async def test_a_long_application_is_processed_normally(
        self, db_session: AsyncSession
    ) -> None:
        portal = await make_portal(db_session, slug="la", options={})

        result = await process_raw_jobs(
            db_session,
            portal=portal,
            raw_jobs=[
                long_application(make_raw_job(portal="la", index=2, now=NOW))
            ],
        )

        assert len(result.processed) == 1
        assert result.rejected == []

    async def test_the_rejection_is_auditable(
        self, db_session: AsyncSession
    ) -> None:
        """A filter that removes listings without saying why is a bug."""
        _, resume = await analysed_resume(db_session)
        portal = await make_portal(db_session, slug="audit", options={})
        prepared = await search_run_service.create_search_run(
            db_session,
            resume=resume,
            portal_ids=[portal.id],
            requested_limit=20,
            require_adapter=False,
        )

        await process_raw_jobs(
            db_session,
            portal=portal,
            raw_jobs=[quick_apply(make_raw_job(portal="audit", index=3, now=NOW))],
            search_run_portal=prepared.legs[0],
        )
        await db_session.flush()

        record = await db_session.scalar(
            select(RawJobRecord).where(
                RawJobRecord.search_run_portal_id == prepared.legs[0].id
            )
        )
        assert record is not None
        assert record.status is RawJobStatus.REJECTED
        assert record.rejection_reason == QUICK_APPLY_REJECTION


# ---------------------------------------------------------------------------
# Ordering: before scoring, before deduplication
# ---------------------------------------------------------------------------


class TestPipelineOrdering:
    async def test_a_quick_apply_job_is_never_scored(
        self, db_session: AsyncSession
    ) -> None:
        _, resume = await analysed_resume(db_session)
        jobs = [
            quick_apply(make_raw_job(portal="p", index=i, now=NOW))
            for i in range(4)
        ]

        _, outcome = await run_over(
            db_session,
            resume=resume,
            specs=[("p", build_fake_portal("p", jobs=jobs))],
        )

        leg = outcome.portals[0]
        assert leg.collected_count == 4
        assert leg.quick_apply_count == 4
        # Never scored: nothing was evaluated, so nothing could qualify.
        assert leg.evaluated_count == 0
        assert leg.qualified_count == 0
        assert outcome.returned_count == 0

    async def test_a_quick_apply_duplicate_cannot_displace_a_real_vacancy(
        self, db_session: AsyncSession
    ) -> None:
        """The filter runs before canonicalisation, so a one-click copy of a
        vacancy can never claim its canonical identity."""
        _, resume = await analysed_resume(db_session)

        shared = dict(
            title="Financial Analyst",
            company="Northwind Capital",
            location="Boston, MA",
            now=NOW,
        )
        real = long_application(
            make_raw_job(portal="real", index=1, **shared)
        )
        clone = quick_apply(make_raw_job(portal="fast", index=1, **shared))

        _, outcome = await run_over(
            db_session,
            resume=resume,
            specs=[
                ("fast", build_fake_portal("fast", jobs=[clone])),
                ("real", build_fake_portal("real", jobs=[real])),
            ],
        )

        assert outcome.returned_count == 1
        item = outcome.results[0]
        # The surviving job came from the portal offering a real application,
        # and carries only that portal as a source.
        assert item.portal_slugs == ("pytest-jobfinder-real",)

        quick_leg = next(
            leg for leg in outcome.portals if leg.portal_slug.endswith("fast")
        )
        assert quick_leg.quick_apply_count == 1
        # It was refused, not merged: it never became a duplicate at all.
        assert quick_leg.duplicate_count == 0

    async def test_no_match_row_is_written_for_a_refused_listing(
        self, db_session: AsyncSession
    ) -> None:
        _, resume = await analysed_resume(db_session)
        jobs = [quick_apply(make_raw_job(portal="p", index=i, now=NOW)) for i in range(3)]

        prepared, _ = await run_over(
            db_session,
            resume=resume,
            specs=[("p", build_fake_portal("p", jobs=jobs))],
        )

        matches = await db_session.scalar(
            select(func.count())
            .select_from(JobMatch)
            .where(JobMatch.search_run_id == prepared.run.id)
        )
        assert matches == 0


# ---------------------------------------------------------------------------
# Mixed batches and the summary
# ---------------------------------------------------------------------------


class TestMixedBatch:
    async def test_only_the_long_applications_come_back(
        self, db_session: AsyncSession
    ) -> None:
        _, resume = await analysed_resume(db_session)
        jobs = [
            long_application(make_raw_job(portal="mix", index=0, company="Alpha", now=NOW)),
            quick_apply(make_raw_job(portal="mix", index=1, company="Beta", now=NOW)),
            long_application(make_raw_job(portal="mix", index=2, company="Gamma", now=NOW)),
            quick_apply(make_raw_job(portal="mix", index=3, company="Delta", now=NOW)),
        ]

        prepared, outcome = await run_over(
            db_session,
            resume=resume,
            specs=[("mix", build_fake_portal("mix", jobs=jobs))],
        )

        companies = {item.company for item in outcome.results}
        assert companies == {"Alpha", "Gamma"}
        assert "Beta" not in companies and "Delta" not in companies

        summary = prepared.run.summary
        assert summary["collected_count"] == 4
        assert summary["quick_apply_excluded_count"] == 2
        assert summary["application_policy"] == LONG_APPLICATION_POLICY
        assert outcome.returned_count == 2

    async def test_the_summary_reports_the_rejection_per_portal(
        self, db_session: AsyncSession
    ) -> None:
        _, resume = await analysed_resume(db_session)

        prepared, _ = await run_over(
            db_session,
            resume=resume,
            specs=[
                (
                    "quick",
                    build_fake_portal(
                        "quick",
                        jobs=[
                            quick_apply(make_raw_job(portal="quick", index=i, now=NOW))
                            for i in range(3)
                        ],
                    ),
                ),
                ("slow", build_fake_portal("slow", jobs=make_jobs("slow", 2, now=NOW))),
            ],
        )

        by_slug = {
            leg["portal"].split("-")[-1]: leg
            for leg in prepared.run.summary["portals"]
        }
        assert by_slug["quick"]["quick_apply_excluded"] == 3
        assert by_slug["quick"]["qualified"] == 0
        assert by_slug["slow"]["quick_apply_excluded"] == 0
        assert by_slug["slow"]["qualified"] == 2

    async def test_the_user_is_told_the_policy_on_every_run(
        self, db_session: AsyncSession
    ) -> None:
        _, resume = await analysed_resume(db_session)

        _, outcome = await run_over(
            db_session,
            resume=resume,
            specs=[("plain", build_fake_portal("plain", jobs=make_jobs("plain", 2, now=NOW)))],
        )

        # Stated whether or not anything was dropped, like the US-only note.
        assert any(
            "real application process" in warning for warning in outcome.warnings
        )

    async def test_a_failed_leg_reports_no_phantom_rejections(
        self, db_session: AsyncSession
    ) -> None:
        from app.portals.exceptions import PortalBlockedError

        _, resume = await analysed_resume(db_session)

        _, outcome = await run_over(
            db_session,
            resume=resume,
            specs=[
                (
                    "broken",
                    build_fake_portal(
                        "broken",
                        jobs=[],
                        error=PortalBlockedError("refused", portal="broken"),
                    ),
                )
            ],
        )

        leg = outcome.portals[0]
        assert leg.collected_count == 0
        assert leg.quick_apply_count == 0


# ---------------------------------------------------------------------------
# Regression: nothing that used to qualify stops qualifying
# ---------------------------------------------------------------------------


class TestNoRegression:
    async def test_undeclared_listings_still_qualify(
        self, db_session: AsyncSession
    ) -> None:
        """Every existing fixture predates this phase and declares nothing.

        They must all still come back: UNKNOWN is eligible, and this filter is
        not allowed to quietly shrink what the product already returned.
        """
        _, resume = await analysed_resume(db_session)

        _, outcome = await run_over(
            db_session,
            resume=resume,
            specs=[("legacy", build_fake_portal("legacy", jobs=make_jobs("legacy", 8, now=NOW)))],
        )

        assert outcome.returned_count == 8
        assert outcome.portals[0].quick_apply_count == 0
        assert outcome.status is SearchRunStatus.COMPLETED

    async def test_scores_are_unchanged_by_the_filter(
        self, db_session: AsyncSession
    ) -> None:
        """A job's score must not depend on whether it declared its type."""
        _, resume = await analysed_resume(db_session)

        _, undeclared = await run_over(
            db_session,
            resume=resume,
            specs=[("a", build_fake_portal("a", jobs=make_jobs("a", 3, now=NOW)))],
        )
        _, declared = await run_over(
            db_session,
            resume=resume,
            specs=[
                (
                    "b",
                    build_fake_portal(
                        "b",
                        jobs=[
                            long_application(job)
                            for job in make_jobs("a", 3, now=NOW)
                        ],
                    ),
                )
            ],
        )

        assert [i.final_score for i in undeclared.results] == [
            i.final_score for i in declared.results
        ]

    async def test_ranking_order_is_unchanged(
        self, db_session: AsyncSession
    ) -> None:
        _, resume = await analysed_resume(db_session)
        jobs = make_jobs("rank", 10, now=NOW) + [
            quick_apply(make_raw_job(portal="rank", index=50 + i, now=NOW))
            for i in range(5)
        ]

        _, outcome = await run_over(
            db_session, resume=resume, specs=[("rank", build_fake_portal("rank", jobs=jobs))]
        )

        scores = [item.ranking_score for item in outcome.results]
        assert scores == sorted(scores, reverse=True)
        assert outcome.returned_count == 10

    async def test_application_history_is_untouched(
        self, db_session: AsyncSession
    ) -> None:
        """This phase adds an eligibility gate; it must not write status."""
        from app.models.application import Application

        _, resume = await analysed_resume(db_session)
        before = await db_session.scalar(
            select(func.count()).select_from(Application)
        )

        await run_over(
            db_session,
            resume=resume,
            specs=[
                (
                    "hist",
                    build_fake_portal(
                        "hist",
                        jobs=make_jobs("hist", 3, now=NOW)
                        + [quick_apply(make_raw_job(portal="hist", index=9, now=NOW))],
                    ),
                )
            ],
        )

        after = await db_session.scalar(
            select(func.count()).select_from(Application)
        )
        assert after == before
