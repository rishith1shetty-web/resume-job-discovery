"""Foundation model definitions.

These assertions are metadata-only and need no database connection, so they
run in the default suite.
"""

from __future__ import annotations

from app.models import Base, Portal, PortalConfig, Resume, User


def test_defined_tables_match_the_completed_phases() -> None:
    # Phase 1 foundation, Phase 2 resume intelligence, Phase 3 job/search/
    # application data - the CLAUDE.md section 12 table set - plus the one
    # table added since for career page decisions.
    assert set(Base.metadata.tables) == {
        # Phase 1
        "users",
        "resumes",
        "portals",
        "portal_configs",
        # Phase 2
        "resume_texts",
        "resume_profiles",
        "resume_skills",
        # Phase 3
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
        # Career page interactions. Not part of the section 12 set, and
        # deliberately its own table rather than a nullable job_id on
        # `applications`: a career page is a company's recruiting site, not a
        # vacancy, so it has no `jobs` row to point at. It stores the user's
        # Applied/Skipped decision, never a discovered page.
        "career_page_interactions",
    }


def test_resumes_are_scoped_to_a_user() -> None:
    # Search results and application history must never mix between resumes,
    # which starts with every resume belonging to exactly one user.
    user_fk = next(iter(Resume.__table__.c.user_id.foreign_keys))

    assert user_fk.column.table.name == "users"
    assert Resume.__table__.c.user_id.nullable is False


def test_reuploading_an_identical_file_is_prevented() -> None:
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in Resume.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }

    assert ("checksum_sha256", "user_id") in constraints


def test_portal_slug_is_unique() -> None:
    assert Portal.__table__.c.slug.unique is True


def test_portals_are_disabled_until_implemented() -> None:
    # A portal must not be searchable before its adapter passes contract tests.
    assert Portal.__table__.c.is_enabled.default.arg is False
    assert Portal.__table__.c.is_implemented.default.arg is False


def test_each_portal_has_exactly_one_config() -> None:
    assert PortalConfig.__table__.c.portal_id.unique is True


def test_portal_config_carries_operational_policy() -> None:
    # Timeouts, retries and rate limits are data, not adapter constants.
    columns = set(PortalConfig.__table__.c.keys())

    assert {
        "timeout_seconds",
        "max_retries",
        "retry_backoff_seconds",
        "rate_limit_per_minute",
        "max_concurrent_requests",
    } <= columns


def test_no_auto_apply_columns_exist_anywhere() -> None:
    forbidden = {
        "easy_apply",
        "apply_priority",
        "unsupported_form",
        "form_progress",
        "auto_submit_result",
        "auto_applied",
    }

    for table in Base.metadata.tables.values():
        assert not (set(table.c.keys()) & forbidden), table.name


def test_all_models_use_uuid_primary_keys() -> None:
    for model in (User, Resume, Portal, PortalConfig):
        pk = list(model.__table__.primary_key.columns)[0]
        assert pk.type.__class__.__name__ == "Uuid", model.__name__


def test_timestamps_are_timezone_aware() -> None:
    for model in (User, Resume, Portal, PortalConfig):
        for column_name in ("created_at", "updated_at"):
            column = model.__table__.c[column_name]
            assert column.type.timezone is True, f"{model.__name__}.{column_name}"
