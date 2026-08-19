"""Career page Applied/Skipped decisions

Gives career pages the same persistent interaction lifecycle jobs already have.

One additive table. Nothing existing is altered, dropped or backfilled, so the
migration is reversible by dropping what it created.

career_page_interactions
    One row per (user, resume, career page). The career page is identified by
    ``career_page_key`` - the canonicalised official career URL, hashed - so the
    same page reached through two portals with different tracking parameters
    resolves to one decision rather than several.

Why a separate table rather than a row in ``applications``: that table's
``job_id`` is NOT NULL and references ``jobs.id`` inside the unique key
``(user_id, resume_id, job_id)``. A career page has no ``jobs`` row, so reusing
it would mean making ``job_id`` nullable and losing the guarantee that every
application points at a real canonical job.

Applied and Skipped are one column on one row, so they cannot both hold. Two
CHECK constraints keep the row honest: the status must be one of those two
decisions, and ``applied_at`` is present exactly when the status is applied.

No status-history table. A career page decision has no pipeline to move
through - it is Applied or Skipped - and an append-only log of a two-valued
field would be recording that the user changed their mind, which nothing asks
for. Jobs keep their history because they do have that pipeline.

Revision ID: 0004_career_page_interactions
Revises: 0003_job_search_application
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004_career_page_interactions'
down_revision: str | None = '0003_job_search_application'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'career_page_interactions',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('resume_id', sa.Uuid(), nullable=False),
        sa.Column('career_page_key', sa.String(length=40), nullable=False),
        sa.Column('company_name', sa.String(length=256), nullable=False),
        sa.Column('official_career_url', sa.String(length=2048), nullable=False),
        sa.Column('relevant_role', sa.String(length=512), nullable=True),
        sa.Column('location', sa.String(length=256), nullable=True),
        sa.Column(
            'status',
            sa.Enum(
                'not_applied',
                'saved',
                'applied',
                'interview',
                'rejected',
                'offer',
                'skipped',
                name='application_status',
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('applied', 'skipped')",
            name=op.f('ck_career_page_interactions_career_page_status_is_a_decision'),
        ),
        sa.CheckConstraint(
            "(status = 'applied') = (applied_at IS NOT NULL)",
            name=op.f(
                'ck_career_page_interactions_career_page_applied_at_matches_status'
            ),
        ),
        sa.ForeignKeyConstraint(
            ['resume_id'],
            ['resumes.id'],
            name=op.f('fk_career_page_interactions_resume_id_resumes'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'],
            ['users.id'],
            name=op.f('fk_career_page_interactions_user_id_users'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_career_page_interactions')),
        sa.UniqueConstraint(
            'user_id',
            'resume_id',
            'career_page_key',
            name='uq_career_page_interactions_resume_page',
        ),
    )
    op.create_index(
        op.f('ix_career_page_interactions_career_page_key'),
        'career_page_interactions',
        ['career_page_key'],
        unique=False,
    )
    op.create_index(
        op.f('ix_career_page_interactions_resume_id'),
        'career_page_interactions',
        ['resume_id'],
        unique=False,
    )
    op.create_index(
        'ix_career_page_interactions_resume_id_status',
        'career_page_interactions',
        ['resume_id', 'status'],
        unique=False,
    )
    op.create_index(
        op.f('ix_career_page_interactions_status'),
        'career_page_interactions',
        ['status'],
        unique=False,
    )
    op.create_index(
        op.f('ix_career_page_interactions_user_id'),
        'career_page_interactions',
        ['user_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f('ix_career_page_interactions_user_id'),
        table_name='career_page_interactions',
    )
    op.drop_index(
        op.f('ix_career_page_interactions_status'),
        table_name='career_page_interactions',
    )
    op.drop_index(
        'ix_career_page_interactions_resume_id_status',
        table_name='career_page_interactions',
    )
    op.drop_index(
        op.f('ix_career_page_interactions_resume_id'),
        table_name='career_page_interactions',
    )
    op.drop_index(
        op.f('ix_career_page_interactions_career_page_key'),
        table_name='career_page_interactions',
    )
    op.drop_table('career_page_interactions')
