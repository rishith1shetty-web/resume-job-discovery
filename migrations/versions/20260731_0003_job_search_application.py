"""Phase 3 job, search and manual application tables

Completes the CLAUDE.md section 12 table set. Ten tables, in three groups:

search_runs / search_run_portals / search_queries / portal_errors
    One run per resume. Quotas, counters, freshness reach and errors live on
    the per-portal row, so one portal failing leaves the run itself intact.

jobs / job_sources / raw_jobs / job_matches
    One canonical job however many portals carry it; one job_sources row per
    portal, keeping that portal's own listing and application URLs. Scores are
    per resume, never per job.

applications / application_status_history
    Manual status only, unique per (user, resume, job) so rediscovering a
    vacancy through a second portal resolves to the existing row instead of a
    new unapplied one. History is append-only and a no-op transition is
    rejected by CHECK rather than trusted to the service layer.

Deliberately not here: EXPIRED as an application status. Expiry is a property
of the job (jobs.expired_at), so a listing going stale can never overwrite a
user-set status.

No data is inserted or modified. Portal rows remain the operator's to create.

Revision ID: 0003_job_search_application
Revises: 0002_resume_profiles
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003_job_search_application'
down_revision: str | None = '0002_resume_profiles'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('jobs',
    sa.Column('canonical_key', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=512), nullable=False),
    sa.Column('normalized_title', sa.String(length=512), nullable=True),
    sa.Column('company', sa.String(length=256), nullable=True),
    sa.Column('normalized_company', sa.String(length=256), nullable=True),
    sa.Column('location', sa.String(length=256), nullable=True),
    sa.Column('work_mode', sa.Enum('onsite', 'hybrid', 'remote', name='work_mode', native_enum=False, create_constraint=True, length=40), nullable=True),
    sa.Column('employment_type', sa.String(length=64), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('description_fingerprint', sa.String(length=64), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_discovered_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expired_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('preferred_application_url', sa.String(length=2048), nullable=True),
    sa.Column('preferred_url_kind', sa.Enum('employer_career', 'ats', 'portal_listing', 'aggregator_redirect', 'unknown', name='job_url_kind', native_enum=False, create_constraint=True, length=40), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs')),
    sa.UniqueConstraint('canonical_key', name=op.f('uq_jobs_canonical_key'))
    )
    op.create_index('ix_jobs_active_last_seen', 'jobs', ['last_seen_at'], unique=False, postgresql_where=sa.text('expired_at IS NULL'))
    op.create_index(op.f('ix_jobs_description_fingerprint'), 'jobs', ['description_fingerprint'], unique=False)
    op.create_index(op.f('ix_jobs_expired_at'), 'jobs', ['expired_at'], unique=False)
    op.create_index('ix_jobs_normalized_company_title', 'jobs', ['normalized_company', 'normalized_title'], unique=False)
    op.create_index(op.f('ix_jobs_normalized_title'), 'jobs', ['normalized_title'], unique=False)
    op.create_index('ix_jobs_posted_at', 'jobs', ['posted_at'], unique=False)
    op.create_table('job_sources',
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('portal_id', sa.Uuid(), nullable=False),
    sa.Column('external_id', sa.String(length=256), nullable=True),
    sa.Column('source_url', sa.String(length=2048), nullable=False),
    sa.Column('application_url', sa.String(length=2048), nullable=True),
    sa.Column('url_kind', sa.Enum('employer_career', 'ats', 'portal_listing', 'aggregator_redirect', 'unknown', name='job_url_kind', native_enum=False, create_constraint=True, length=40), nullable=False),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('is_preferred', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_job_sources_job_id_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['portal_id'], ['portals.id'], name=op.f('fk_job_sources_portal_id_portals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_sources')),
    sa.UniqueConstraint('job_id', 'portal_id', name=op.f('uq_job_sources_job_id_portal_id'))
    )
    op.create_index(op.f('ix_job_sources_job_id'), 'job_sources', ['job_id'], unique=False)
    op.create_index(op.f('ix_job_sources_portal_id'), 'job_sources', ['portal_id'], unique=False)
    op.create_index('uq_job_sources_portal_external_id', 'job_sources', ['portal_id', 'external_id'], unique=True, postgresql_where=sa.text('external_id IS NOT NULL'))
    op.create_table('applications',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('resume_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('status', sa.Enum('not_applied', 'saved', 'applied', 'interview', 'rejected', 'offer', 'skipped', name='application_status', native_enum=False, create_constraint=True, length=40), nullable=False),
    sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status NOT IN ('applied', 'interview', 'offer', 'rejected') OR applied_at IS NOT NULL", name=op.f('ck_applications_applied_at_required_when_submitted')),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_applications_job_id_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resume_id'], ['resumes.id'], name=op.f('fk_applications_resume_id_resumes'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_applications_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_applications')),
    sa.UniqueConstraint('user_id', 'resume_id', 'job_id', name=op.f('uq_applications_user_id_resume_id_job_id'))
    )
    op.create_index(op.f('ix_applications_job_id'), 'applications', ['job_id'], unique=False)
    op.create_index(op.f('ix_applications_resume_id'), 'applications', ['resume_id'], unique=False)
    op.create_index('ix_applications_resume_id_status', 'applications', ['resume_id', 'status'], unique=False)
    op.create_index(op.f('ix_applications_status'), 'applications', ['status'], unique=False)
    op.create_index(op.f('ix_applications_user_id'), 'applications', ['user_id'], unique=False)
    op.create_table('application_status_history',
    sa.Column('application_id', sa.Uuid(), nullable=False),
    sa.Column('old_status', sa.Enum('not_applied', 'saved', 'applied', 'interview', 'rejected', 'offer', 'skipped', name='application_status_old', native_enum=False, create_constraint=True, length=40), nullable=True),
    sa.Column('new_status', sa.Enum('not_applied', 'saved', 'applied', 'interview', 'rejected', 'offer', 'skipped', name='application_status_new', native_enum=False, create_constraint=True, length=40), nullable=False),
    sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('changed_by', sa.String(length=32), server_default='user', nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('old_status IS DISTINCT FROM new_status', name=op.f('ck_application_status_history_status_actually_changed')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_application_status_history_application_id_applications'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_application_status_history'))
    )
    op.create_index(op.f('ix_application_status_history_application_id'), 'application_status_history', ['application_id'], unique=False)
    op.create_index('ix_application_status_history_application_id_changed_at', 'application_status_history', ['application_id', 'changed_at'], unique=False)
    op.create_table('search_runs',
    sa.Column('resume_id', sa.Uuid(), nullable=False),
    sa.Column('profile_id', sa.Uuid(), nullable=True),
    sa.Column('requested_limit', sa.Integer(), nullable=False),
    sa.Column('min_score', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'running', 'completed', 'completed_with_errors', 'failed', 'cancelled', name='search_run_status', native_enum=False, create_constraint=True, length=40), nullable=False),
    sa.Column('searched_domain', sa.String(length=64), nullable=True),
    sa.Column('searched_target_role', sa.String(length=64), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('min_score >= 70 AND min_score <= 100', name=op.f('ck_search_runs_min_score_locked')),
    sa.CheckConstraint('requested_limit IN (20, 50, 100)', name=op.f('ck_search_runs_requested_limit_allowed')),
    sa.ForeignKeyConstraint(['profile_id'], ['resume_profiles.id'], name=op.f('fk_search_runs_profile_id_resume_profiles'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resume_id'], ['resumes.id'], name=op.f('fk_search_runs_resume_id_resumes'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_search_runs'))
    )
    op.create_index(op.f('ix_search_runs_resume_id'), 'search_runs', ['resume_id'], unique=False)
    op.create_index('ix_search_runs_resume_id_created_at', 'search_runs', ['resume_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_search_runs_status'), 'search_runs', ['status'], unique=False)
    op.create_table('job_matches',
    sa.Column('resume_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('search_run_id', sa.Uuid(), nullable=True),
    sa.Column('final_score', sa.Float(), nullable=False),
    sa.Column('role_score', sa.Float(), nullable=False),
    sa.Column('skill_score', sa.Float(), nullable=False),
    sa.Column('experience_score', sa.Float(), nullable=False),
    sa.Column('responsibility_score', sa.Float(), nullable=False),
    sa.Column('domain_score', sa.Float(), nullable=False),
    sa.Column('education_score', sa.Float(), nullable=False),
    sa.Column('freshness_score', sa.Float(), nullable=False),
    sa.Column('ranking_score', sa.Float(), nullable=False),
    sa.Column('qualified', sa.Boolean(), nullable=False),
    sa.Column('rejection_reasons', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('score_details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('scoring_version', sa.String(length=32), nullable=False),
    sa.Column('matched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('final_score BETWEEN 0.0 AND 100.0', name=op.f('ck_job_matches_final_score_range')),
    sa.CheckConstraint('role_score BETWEEN 0.0 AND 100.0', name=op.f('ck_job_matches_role_score_range')),
    sa.CheckConstraint('skill_score BETWEEN 0.0 AND 100.0', name=op.f('ck_job_matches_skill_score_range')),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_job_matches_job_id_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['resume_id'], ['resumes.id'], name=op.f('fk_job_matches_resume_id_resumes'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['search_run_id'], ['search_runs.id'], name=op.f('fk_job_matches_search_run_id_search_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_matches')),
    sa.UniqueConstraint('resume_id', 'job_id', 'search_run_id', name=op.f('uq_job_matches_resume_id_job_id_search_run_id'))
    )
    op.create_index(op.f('ix_job_matches_job_id'), 'job_matches', ['job_id'], unique=False)
    op.create_index(op.f('ix_job_matches_qualified'), 'job_matches', ['qualified'], unique=False)
    op.create_index(op.f('ix_job_matches_resume_id'), 'job_matches', ['resume_id'], unique=False)
    op.create_index('ix_job_matches_resume_qualified_score', 'job_matches', ['resume_id', 'qualified', 'ranking_score'], unique=False)
    op.create_index('ix_job_matches_search_run_id_qualified', 'job_matches', ['search_run_id', 'qualified'], unique=False)
    op.create_table('search_run_portals',
    sa.Column('search_run_id', sa.Uuid(), nullable=False),
    sa.Column('portal_id', sa.Uuid(), nullable=False),
    sa.Column('target_quota', sa.Integer(), nullable=False),
    sa.Column('final_quota', sa.Integer(), nullable=False),
    sa.Column('raw_count', sa.Integer(), nullable=False),
    sa.Column('qualified_count', sa.Integer(), nullable=False),
    sa.Column('selected_count', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'running', 'completed', 'failed', 'skipped', name='portal_run_status', native_enum=False, create_constraint=True, length=40), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('deepest_window', sa.Enum('last_24h', 'last_3d', 'last_7d', 'last_14d', 'last_30d', 'older_active', name='freshness_window', native_enum=False, create_constraint=True, length=40), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('selected_count >= 0', name=op.f('ck_search_run_portals_selected_count_non_negative')),
    sa.CheckConstraint('target_quota >= 0', name=op.f('ck_search_run_portals_target_quota_non_negative')),
    sa.ForeignKeyConstraint(['portal_id'], ['portals.id'], name=op.f('fk_search_run_portals_portal_id_portals'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['search_run_id'], ['search_runs.id'], name=op.f('fk_search_run_portals_search_run_id_search_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_search_run_portals')),
    sa.UniqueConstraint('search_run_id', 'portal_id', name=op.f('uq_search_run_portals_search_run_id_portal_id'))
    )
    op.create_index(op.f('ix_search_run_portals_portal_id'), 'search_run_portals', ['portal_id'], unique=False)
    op.create_index(op.f('ix_search_run_portals_search_run_id'), 'search_run_portals', ['search_run_id'], unique=False)
    op.create_index('ix_search_run_portals_status', 'search_run_portals', ['status'], unique=False)
    op.create_table('portal_errors',
    sa.Column('portal_id', sa.Uuid(), nullable=False),
    sa.Column('search_run_id', sa.Uuid(), nullable=True),
    sa.Column('search_run_portal_id', sa.Uuid(), nullable=True),
    sa.Column('kind', sa.Enum('timeout', 'rate_limited', 'auth_required', 'blocked', 'network', 'parse', 'not_implemented', 'unknown', name='portal_error_kind', native_enum=False, create_constraint=True, length=40), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('http_status', sa.Integer(), nullable=True),
    sa.Column('url', sa.String(length=2048), nullable=True),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['portal_id'], ['portals.id'], name=op.f('fk_portal_errors_portal_id_portals'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['search_run_id'], ['search_runs.id'], name=op.f('fk_portal_errors_search_run_id_search_runs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['search_run_portal_id'], ['search_run_portals.id'], name=op.f('fk_portal_errors_search_run_portal_id_search_run_portals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_portal_errors'))
    )
    op.create_index(op.f('ix_portal_errors_portal_id'), 'portal_errors', ['portal_id'], unique=False)
    op.create_index('ix_portal_errors_portal_id_created_at', 'portal_errors', ['portal_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_portal_errors_search_run_id'), 'portal_errors', ['search_run_id'], unique=False)
    op.create_table('search_queries',
    sa.Column('search_run_portal_id', sa.Uuid(), nullable=False),
    sa.Column('query_text', sa.String(length=256), nullable=False),
    sa.Column('window', sa.Enum('last_24h', 'last_3d', 'last_7d', 'last_14d', 'last_30d', 'older_active', name='freshness_window', native_enum=False, create_constraint=True, length=40), nullable=False),
    sa.Column('page', sa.Integer(), nullable=False),
    sa.Column('result_count', sa.Integer(), nullable=False),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['search_run_portal_id'], ['search_run_portals.id'], name=op.f('fk_search_queries_search_run_portal_id_search_run_portals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_search_queries'))
    )
    op.create_index('ix_search_queries_portal_leg_window', 'search_queries', ['search_run_portal_id', 'window'], unique=False)
    op.create_index(op.f('ix_search_queries_search_run_portal_id'), 'search_queries', ['search_run_portal_id'], unique=False)
    op.create_table('raw_jobs',
    sa.Column('search_run_portal_id', sa.Uuid(), nullable=False),
    sa.Column('portal_id', sa.Uuid(), nullable=False),
    sa.Column('search_query_id', sa.Uuid(), nullable=True),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('external_id', sa.String(length=256), nullable=True),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=512), nullable=False),
    sa.Column('company', sa.String(length=256), nullable=True),
    sa.Column('location', sa.String(length=256), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('source_url', sa.String(length=2048), nullable=False),
    sa.Column('application_url', sa.String(length=2048), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('employment_type', sa.String(length=64), nullable=True),
    sa.Column('raw_payload', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.Enum('collected', 'normalized', 'duplicate', 'rejected', 'failed', name='raw_job_status', native_enum=False, create_constraint=True, length=40), nullable=False),
    sa.Column('rejection_reason', sa.String(length=128), nullable=True),
    sa.Column('collected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_raw_jobs_job_id_jobs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['portal_id'], ['portals.id'], name=op.f('fk_raw_jobs_portal_id_portals'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['search_query_id'], ['search_queries.id'], name=op.f('fk_raw_jobs_search_query_id_search_queries'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['search_run_portal_id'], ['search_run_portals.id'], name=op.f('fk_raw_jobs_search_run_portal_id_search_run_portals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_raw_jobs'))
    )
    op.create_index(op.f('ix_raw_jobs_job_id'), 'raw_jobs', ['job_id'], unique=False)
    op.create_index('ix_raw_jobs_leg_status', 'raw_jobs', ['search_run_portal_id', 'status'], unique=False)
    op.create_index('ix_raw_jobs_portal_external_id', 'raw_jobs', ['portal_id', 'external_id'], unique=False)
    op.create_index(op.f('ix_raw_jobs_search_run_portal_id'), 'raw_jobs', ['search_run_portal_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_raw_jobs_search_run_portal_id'), table_name='raw_jobs')
    op.drop_index('ix_raw_jobs_portal_external_id', table_name='raw_jobs')
    op.drop_index('ix_raw_jobs_leg_status', table_name='raw_jobs')
    op.drop_index(op.f('ix_raw_jobs_job_id'), table_name='raw_jobs')
    op.drop_table('raw_jobs')
    op.drop_index(op.f('ix_search_queries_search_run_portal_id'), table_name='search_queries')
    op.drop_index('ix_search_queries_portal_leg_window', table_name='search_queries')
    op.drop_table('search_queries')
    op.drop_index(op.f('ix_portal_errors_search_run_id'), table_name='portal_errors')
    op.drop_index('ix_portal_errors_portal_id_created_at', table_name='portal_errors')
    op.drop_index(op.f('ix_portal_errors_portal_id'), table_name='portal_errors')
    op.drop_table('portal_errors')
    op.drop_index('ix_search_run_portals_status', table_name='search_run_portals')
    op.drop_index(op.f('ix_search_run_portals_search_run_id'), table_name='search_run_portals')
    op.drop_index(op.f('ix_search_run_portals_portal_id'), table_name='search_run_portals')
    op.drop_table('search_run_portals')
    op.drop_index('ix_job_matches_search_run_id_qualified', table_name='job_matches')
    op.drop_index('ix_job_matches_resume_qualified_score', table_name='job_matches')
    op.drop_index(op.f('ix_job_matches_resume_id'), table_name='job_matches')
    op.drop_index(op.f('ix_job_matches_qualified'), table_name='job_matches')
    op.drop_index(op.f('ix_job_matches_job_id'), table_name='job_matches')
    op.drop_table('job_matches')
    op.drop_index(op.f('ix_search_runs_status'), table_name='search_runs')
    op.drop_index('ix_search_runs_resume_id_created_at', table_name='search_runs')
    op.drop_index(op.f('ix_search_runs_resume_id'), table_name='search_runs')
    op.drop_table('search_runs')
    op.drop_index('ix_application_status_history_application_id_changed_at', table_name='application_status_history')
    op.drop_index(op.f('ix_application_status_history_application_id'), table_name='application_status_history')
    op.drop_table('application_status_history')
    op.drop_index(op.f('ix_applications_user_id'), table_name='applications')
    op.drop_index(op.f('ix_applications_status'), table_name='applications')
    op.drop_index('ix_applications_resume_id_status', table_name='applications')
    op.drop_index(op.f('ix_applications_resume_id'), table_name='applications')
    op.drop_index(op.f('ix_applications_job_id'), table_name='applications')
    op.drop_table('applications')
    op.drop_index('uq_job_sources_portal_external_id', table_name='job_sources', postgresql_where=sa.text('external_id IS NOT NULL'))
    op.drop_index(op.f('ix_job_sources_portal_id'), table_name='job_sources')
    op.drop_index(op.f('ix_job_sources_job_id'), table_name='job_sources')
    op.drop_table('job_sources')
    op.drop_index('ix_jobs_posted_at', table_name='jobs')
    op.drop_index(op.f('ix_jobs_normalized_title'), table_name='jobs')
    op.drop_index('ix_jobs_normalized_company_title', table_name='jobs')
    op.drop_index(op.f('ix_jobs_expired_at'), table_name='jobs')
    op.drop_index(op.f('ix_jobs_description_fingerprint'), table_name='jobs')
    op.drop_index('ix_jobs_active_last_seen', table_name='jobs', postgresql_where=sa.text('expired_at IS NULL'))
    op.drop_table('jobs')
