"""Phase 1 foundation tables

Creates users, resumes, portals and portal_configs.

The remaining CLAUDE.md section 12 tables - resume_profiles, search_runs,
search_run_portals, search_queries, raw_jobs, jobs, job_sources, job_matches,
applications, application_status_history, portal_errors - are added in Phase 3.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-07-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "resumes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("storage_path", sa.String(length=1024), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "uploaded",
                "parsing",
                "parsed",
                "failed",
                name="resume_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_resumes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resumes")),
        # Re-uploading a byte-identical file resolves to the existing resume
        # instead of fragmenting that resume's search history.
        sa.UniqueConstraint(
            "user_id",
            "checksum_sha256",
            name=op.f("uq_resumes_user_id_checksum_sha256"),
        ),
    )
    op.create_index(op.f("ix_resumes_user_id"), "resumes", ["user_id"])
    op.create_index(op.f("ix_resumes_status"), "resumes", ["status"])

    op.create_table(
        "portals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("website_url", sa.String(length=512), nullable=True),
        sa.Column("adapter_key", sa.String(length=64), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("is_implemented", sa.Boolean(), nullable=False),
        sa.Column("supports_date_filter", sa.Boolean(), nullable=False),
        sa.Column("supports_description_fetch", sa.Boolean(), nullable=False),
        sa.Column("requires_auth", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portals")),
        sa.UniqueConstraint("slug", name=op.f("uq_portals_slug")),
    )
    op.create_index(op.f("ix_portals_is_enabled"), "portals", ["is_enabled"])

    op.create_table(
        "portal_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("portal_id", sa.Uuid(), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("retry_backoff_seconds", sa.Float(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("max_concurrent_requests", sa.Integer(), nullable=False),
        sa.Column("max_pages_per_window", sa.Integer(), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["portal_id"],
            ["portals.id"],
            name=op.f("fk_portal_configs_portal_id_portals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_portal_configs")),
        sa.UniqueConstraint("portal_id", name=op.f("uq_portal_configs_portal_id")),
    )


def downgrade() -> None:
    op.drop_table("portal_configs")
    op.drop_index(op.f("ix_portals_is_enabled"), table_name="portals")
    op.drop_table("portals")
    op.drop_index(op.f("ix_resumes_status"), table_name="resumes")
    op.drop_index(op.f("ix_resumes_user_id"), table_name="resumes")
    op.drop_table("resumes")
    op.drop_table("users")
