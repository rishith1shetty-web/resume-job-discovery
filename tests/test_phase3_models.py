"""Phase 3 schema guarantees.

Metadata-only assertions: no database connection needed, so these run in the
default suite and fail fast if a later change loosens a rule the product
depends on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.core.constants import ApplicationStatus, SearchRunStatus
from app.models import (
    Application,
    ApplicationStatusHistory,
    Base,
    Job,
    JobMatch,
    JobSource,
    RawJobRecord,
    SearchRun,
    SearchRunPortal,
)

PHASE_3_TABLES = {
    "search_runs",
    "search_run_portals",
    "search_queries",
    "raw_jobs",
    "jobs",
    "job_sources",
    "job_matches",
    "applications",
    "application_status_history",
    "portal_errors",
}


def _unique_constraints(model: type) -> set[tuple[str, ...]]:
    return {
        tuple(sorted(constraint.columns.keys()))
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def _check_constraint_text(model: type) -> str:
    return " ".join(
        str(constraint.sqltext).lower()
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )


# --- Table set --------------------------------------------------------------


def test_all_claude_md_section_12_tables_now_exist() -> None:
    # The exact table set is asserted once, in test_models.py.
    assert PHASE_3_TABLES <= set(Base.metadata.tables)


# --- Resume-wise separation -------------------------------------------------


def test_every_search_run_belongs_to_one_resume() -> None:
    column = SearchRun.__table__.c.resume_id
    fk = next(iter(column.foreign_keys))

    assert column.nullable is False
    assert fk.column.table.name == "resumes"
    assert fk.ondelete == "CASCADE"


def test_a_job_match_belongs_to_one_resume_and_one_job() -> None:
    for name in ("resume_id", "job_id"):
        assert JobMatch.__table__.c[name].nullable is False

    # One verdict per resume per job per run: a rerun records a new judgement
    # rather than overwriting the previous one.
    assert ("job_id", "resume_id", "search_run_id") in _unique_constraints(JobMatch)


def test_application_status_is_scoped_by_user_resume_and_job() -> None:
    # The constraint that stops one resume's application state leaking into
    # another's, and stops a rediscovery creating a second row.
    assert ("job_id", "resume_id", "user_id") in _unique_constraints(Application)

    for name in ("user_id", "resume_id", "job_id"):
        assert Application.__table__.c[name].nullable is False


def test_a_canonical_job_is_not_scoped_to_a_user_or_resume() -> None:
    # One row per real vacancy, shared across resumes. Everything
    # resume-specific lives in job_matches and applications.
    assert "user_id" not in Job.__table__.c
    assert "resume_id" not in Job.__table__.c


# --- Canonical job and sources ----------------------------------------------


def test_a_job_has_one_source_row_per_portal() -> None:
    assert ("job_id", "portal_id") in _unique_constraints(JobSource)


def test_source_and_application_urls_are_stored_separately() -> None:
    columns = JobSource.__table__.c

    # Where the listing was found and where the user applies are different
    # facts; collapsing them would lose the direct application link.
    assert columns["source_url"].nullable is False
    assert "application_url" in columns
    assert "url_kind" in columns


def test_canonical_key_is_unique() -> None:
    assert ("canonical_key",) in _unique_constraints(Job)


def test_raw_captures_are_kept_verbatim() -> None:
    columns = set(RawJobRecord.__table__.c.keys())

    # Mirrors app.portals.base.RawJob so nothing a portal returned is dropped.
    assert {
        "external_id",
        "source",
        "title",
        "company",
        "location",
        "description",
        "source_url",
        "application_url",
        "posted_at",
        "employment_type",
        "raw_payload",
    } <= columns


# --- Search-run rules -------------------------------------------------------


def test_requested_limit_allows_only_20_50_or_100() -> None:
    text = _check_constraint_text(SearchRun)

    assert "requested_limit in (20, 50, 100)" in text


def test_min_score_cannot_be_lowered_below_70() -> None:
    text = _check_constraint_text(SearchRun)

    assert "min_score >= 70" in text


def test_search_run_supports_every_required_state() -> None:
    values = set(SearchRun.__table__.c.status.type.enums)

    assert values == {
        "pending",
        "running",
        "completed",
        "completed_with_errors",
        "failed",
        "cancelled",
    }
    assert {status.value for status in SearchRunStatus} == values


def test_portal_leg_records_quota_counters_and_errors() -> None:
    columns = set(SearchRunPortal.__table__.c.keys())

    assert {
        "target_quota",
        "final_quota",
        "raw_count",
        "qualified_count",
        "selected_count",
        "status",
        "error",
    } <= columns


def test_a_portal_appears_at_most_once_per_run() -> None:
    assert ("portal_id", "search_run_id") in _unique_constraints(SearchRunPortal)


# --- Match components -------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "final_score",
        "role_score",
        "skill_score",
        "experience_score",
        "responsibility_score",
        "domain_score",
        "education_score",
        "freshness_score",
        "ranking_score",
        "qualified",
        "rejection_reasons",
        "scoring_version",
    ],
)
def test_job_match_stores_every_required_field(column: str) -> None:
    assert column in JobMatch.__table__.c


def test_scoring_version_is_mandatory() -> None:
    # A score with no version is untraceable once the logic changes.
    assert JobMatch.__table__.c.scoring_version.nullable is False


# --- Application status rules -----------------------------------------------


def test_application_statuses_are_exactly_the_seven_manual_ones() -> None:
    values = set(Application.__table__.c.status.type.enums)

    assert values == {
        "not_applied",
        "saved",
        "applied",
        "interview",
        "rejected",
        "offer",
        "skipped",
    }


def test_expired_is_not_an_application_status() -> None:
    # Expiry is a property of the job. If it were a status, a listing going
    # stale could overwrite APPLIED.
    assert "expired" not in {status.value for status in ApplicationStatus}
    assert "expired" not in set(Application.__table__.c.status.type.enums)


def test_expiry_lives_on_the_job() -> None:
    assert "expired_at" in Job.__table__.c
    assert "expired_at" not in Application.__table__.c


def test_a_submitted_application_must_record_when() -> None:
    text = _check_constraint_text(Application)

    assert "applied_at is not null" in text


def test_history_rejects_a_no_op_transition() -> None:
    text = _check_constraint_text(ApplicationStatusHistory)

    assert "old_status is distinct from new_status" in text


def test_history_is_append_only() -> None:
    columns = set(ApplicationStatusHistory.__table__.c.keys())

    # No updated_at: rows are written once and never edited.
    assert "updated_at" not in columns
    assert {"old_status", "new_status", "changed_at"} <= columns


def test_first_history_row_may_have_no_previous_status() -> None:
    assert ApplicationStatusHistory.__table__.c.old_status.nullable is True
    assert ApplicationStatusHistory.__table__.c.new_status.nullable is False


# --- Freshness --------------------------------------------------------------


@pytest.mark.parametrize(
    "column",
    [
        "posted_at",
        "first_discovered_at",
        "last_seen_at",
        "last_verified_at",
        "expired_at",
    ],
)
def test_job_stores_every_freshness_timestamp(column: str) -> None:
    assert column in Job.__table__.c
    assert Job.__table__.c[column].type.timezone is True


# --- Auto-apply guard -------------------------------------------------------


def test_no_auto_apply_columns_were_introduced() -> None:
    forbidden = {
        "easy_apply",
        "apply_priority",
        "unsupported_form",
        "form_progress",
        "auto_submit_result",
        "auto_applied",
        "quick_apply",
    }

    for table in Base.metadata.tables.values():
        assert not (set(table.c.keys()) & forbidden), table.name
