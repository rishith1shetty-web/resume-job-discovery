"""Migration/model drift guard.

Runs Alembic in offline (``--sql``) mode, so it needs no database connection
but still proves the migration produces what the models declare.
"""

from __future__ import annotations

import io

import pytest
from alembic import command
from alembic.config import Config

from app.core.config import PROJECT_ROOT
from app.models import Base


@pytest.fixture(scope="module")
def migration_sql() -> str:
    buffer = io.StringIO()
    # `output_buffer` receives the generated SQL; `stdout` only gets log lines.
    config = Config(
        str(PROJECT_ROOT / "alembic.ini"),
        output_buffer=buffer,
        stdout=io.StringIO(),
    )
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))

    command.upgrade(config, "head", sql=True)

    return buffer.getvalue().lower()


def test_every_model_table_is_created(migration_sql: str) -> None:
    for table_name in Base.metadata.tables:
        assert f"create table {table_name}" in migration_sql, table_name


def test_every_model_column_is_created(migration_sql: str) -> None:
    for table in Base.metadata.tables.values():
        for column in table.c:
            assert column.name in migration_sql, f"{table.name}.{column.name}"


def test_foreign_keys_cascade_on_delete(migration_sql: str) -> None:
    # Deleting a user must not strand their resumes, and deleting a resume
    # must not strand its extracted text, profiles or skills.
    for constraint in (
        "fk_resumes_user_id_users",
        "fk_portal_configs_portal_id_portals",
        "fk_resume_texts_resume_id_resumes",
        "fk_resume_profiles_resume_id_resumes",
        "fk_resume_skills_profile_id_resume_profiles",
    ):
        assert constraint in migration_sql, constraint

    # Every foreign key in the schema cascades; none are left to orphan rows.
    assert migration_sql.count("on delete cascade") == migration_sql.count(
        "foreign key"
    )


def test_resume_status_values_are_constrained(migration_sql: str) -> None:
    assert "ck_resumes_resume_status" in migration_sql
    for status in ("uploaded", "parsing", "parsed", "failed"):
        assert f"'{status}'" in migration_sql


def test_uniqueness_rules_are_enforced(migration_sql: str) -> None:
    for constraint in (
        "uq_users_email",
        "uq_portals_slug",
        "uq_portal_configs_portal_id",
        "uq_resumes_user_id_checksum_sha256",
    ):
        assert constraint in migration_sql, constraint


def test_timestamps_are_timezone_aware_in_sql(migration_sql: str) -> None:
    assert "timestamp with time zone" in migration_sql
    assert "timestamp without time zone" not in migration_sql


def test_migration_targets_a_single_head() -> None:
    from alembic.script import ScriptDirectory

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))

    # Multiple heads mean a branched history that `upgrade head` cannot resolve.
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
