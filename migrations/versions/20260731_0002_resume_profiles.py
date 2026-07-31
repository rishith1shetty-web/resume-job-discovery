"""Phase 2 resume intelligence tables

Adds resume_texts, resume_profiles and resume_skills.

resume_profiles is versioned rather than overwritten: each analysis run
inserts a row carrying its analysis_version, and a partial unique index keeps
exactly one active profile per resume while superseded versions remain
queryable for debugging a wrong detection.

Revision ID: 0002_resume_profiles
Revises: 0001_foundation
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0002_resume_profiles'
down_revision: str | None = '0001_foundation'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:

    op.create_table('resume_profiles',
    sa.Column('resume_id', sa.Uuid(), nullable=False),
    sa.Column('analysis_version', sa.String(length=32), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('analyzed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('detected_domain', sa.String(length=64), nullable=True),
    sa.Column('detected_domain_display', sa.String(length=128), nullable=True),
    sa.Column('domain_confidence', sa.Float(), nullable=False),
    sa.Column('domain_evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('detected_target_role', sa.String(length=64), nullable=True),
    sa.Column('detected_target_role_display', sa.String(length=128), nullable=True),
    sa.Column('target_role_confidence', sa.Float(), nullable=False),
    sa.Column('target_role_evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('confirmed_domain', sa.String(length=64), nullable=True),
    sa.Column('confirmed_target_role', sa.String(length=64), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('seniority', sa.Enum('intern', 'entry', 'junior', 'mid', 'senior', 'lead', 'principal', 'manager', 'director', 'executive', name='seniority_level', native_enum=False, create_constraint=True, length=32), nullable=True),
    sa.Column('seniority_confidence', sa.Float(), nullable=False),
    sa.Column('seniority_evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('years_experience', sa.Float(), nullable=True),
    sa.Column('years_experience_source', sa.String(length=32), nullable=True),
    sa.Column('years_experience_evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('role_variations', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('titles', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('responsibilities', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('education', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('certifications', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('sections_found', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['resume_id'], ['resumes.id'], name=op.f('fk_resume_profiles_resume_id_resumes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resume_profiles'))
    )
    op.create_index(op.f('ix_resume_profiles_is_active'), 'resume_profiles', ['is_active'], unique=False)
    op.create_index(op.f('ix_resume_profiles_resume_id'), 'resume_profiles', ['resume_id'], unique=False)
    op.create_index('ix_resume_profiles_resume_id_created_at', 'resume_profiles', ['resume_id', 'created_at'], unique=False)
    op.create_index('uq_resume_profiles_active_per_resume', 'resume_profiles', ['resume_id'], unique=True, postgresql_where=sa.text('is_active'))
    op.create_table('resume_texts',
    sa.Column('resume_id', sa.Uuid(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('extractor', sa.String(length=64), nullable=False),
    sa.Column('extractor_version', sa.String(length=64), nullable=False),
    sa.Column('warnings', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['resume_id'], ['resumes.id'], name=op.f('fk_resume_texts_resume_id_resumes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resume_texts')),
    sa.UniqueConstraint('resume_id', name=op.f('uq_resume_texts_resume_id'))
    )
    op.create_table('resume_skills',
    sa.Column('profile_id', sa.Uuid(), nullable=False),
    sa.Column('skill_key', sa.String(length=64), nullable=False),
    sa.Column('display', sa.String(length=128), nullable=False),
    sa.Column('kind', sa.Enum('skill', 'technology', 'tool', name='skill_kind', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('occurrences', sa.Integer(), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('sections', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('evidence', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['profile_id'], ['resume_profiles.id'], name=op.f('fk_resume_skills_profile_id_resume_profiles'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resume_skills')),
    sa.UniqueConstraint('profile_id', 'skill_key', name=op.f('uq_resume_skills_profile_id_skill_key'))
    )
    op.create_index(op.f('ix_resume_skills_profile_id'), 'resume_skills', ['profile_id'], unique=False)
    op.create_index('ix_resume_skills_skill_key', 'resume_skills', ['skill_key'], unique=False)



def downgrade() -> None:

    op.drop_index('ix_resume_skills_skill_key', table_name='resume_skills')
    op.drop_index(op.f('ix_resume_skills_profile_id'), table_name='resume_skills')
    op.drop_table('resume_skills')
    op.drop_table('resume_texts')
    op.drop_index('uq_resume_profiles_active_per_resume', table_name='resume_profiles', postgresql_where=sa.text('is_active'))
    op.drop_index('ix_resume_profiles_resume_id_created_at', table_name='resume_profiles')
    op.drop_index(op.f('ix_resume_profiles_resume_id'), table_name='resume_profiles')
    op.drop_index(op.f('ix_resume_profiles_is_active'), table_name='resume_profiles')
    op.drop_table('resume_profiles')

