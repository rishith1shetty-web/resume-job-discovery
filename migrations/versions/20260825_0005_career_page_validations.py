"""Career page validation cache

One additive table. Nothing existing is altered, dropped or backfilled, so the
migration is reversible by dropping what it created, and an existing database
upgrades without touching a single row of anyone's data.

career_page_validations
    What fetching a company's career page established, and when. Career page
    verification now opens the candidate URL rather than judging its spelling,
    which means real HTTP requests to employers' sites. This table is what
    stops a run re-opening eighty employers' sites it checked an hour ago.

It is a **cache and nothing else**. No result is read from it that could not be
recomputed by fetching again, no user decision lives here, and truncating it
costs a run some requests and nothing more. That is why there is no foreign key
to a resume or a user: a verdict about an employer's careers page is a fact
about that page, identical for every user who reaches it.

Keyed on ``career_page_key`` from ``app.careers.identity`` - the employer plus
the host their careers presence lives on - which is the same identity the
Applied/Skipped decisions in ``career_page_interactions`` use. Two runs that
reach one employer through two different postings therefore share one row.

Freshness is decided in Python from ``status`` and ``last_validated_at`` rather
than by a stored expiry column, because the three time-to-live values differ by
outcome and are policy: a proven page is trusted for hours, a rejected one for
days, and a "we could not look at it" for minutes, so that a temporary block
can never permanently remove an employer from the results.

Revision ID: 0005_career_page_validations
Revises: 0004_career_page_interactions
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0005_career_page_validations'
down_revision: str | None = '0004_career_page_interactions'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'career_page_validations',
        sa.Column('career_page_key', sa.String(length=40), nullable=False),
        sa.Column('company_name', sa.String(length=256), nullable=False),
        sa.Column('candidate_url', sa.String(length=2048), nullable=False),
        sa.Column('verified_url', sa.String(length=2048), nullable=True),
        sa.Column('url_kind', sa.String(length=40), nullable=True),
        sa.Column(
            'status',
            sa.Enum(
                'discovered',
                'validating',
                'verified',
                'rejected',
                'unavailable',
                'unverified',
                name='career_page_status',
                native_enum=False,
                create_constraint=True,
                length=40,
            ),
            nullable=False,
        ),
        sa.Column('validation_reason', sa.Text(), nullable=True),
        sa.Column('last_validated_at', sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint('id', name=op.f('pk_career_page_validations')),
    )
    # Unique: one cached verdict per career page. An upsert relies on it.
    op.create_index(
        'ix_career_page_validations_key',
        'career_page_validations',
        ['career_page_key'],
        unique=True,
    )
    op.create_index(
        'ix_career_page_validations_status',
        'career_page_validations',
        ['status'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'ix_career_page_validations_status', table_name='career_page_validations'
    )
    op.drop_index(
        'ix_career_page_validations_key', table_name='career_page_validations'
    )
    op.drop_table('career_page_validations')
