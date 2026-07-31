"""SQLAlchemy models.

Every model must be imported here: Alembic autogenerate only sees tables that
are attached to ``Base.metadata`` at import time.

Phase 1 defined the foundation (users, resumes, portals, portal_configs).
Phase 2 adds resume_texts, resume_profiles and resume_skills.

The remaining CLAUDE.md section 12 tables - search_runs, search_run_portals,
search_queries, raw_jobs, jobs, job_sources, job_matches, applications,
application_status_history, portal_errors - land in Phase 3.
"""

from app.db.base import Base
from app.models.portal import Portal, PortalConfig
from app.models.profile import ResumeProfile, ResumeSkill, ResumeText
from app.models.resume import Resume
from app.models.user import User

__all__ = [
    "Base",
    "Portal",
    "PortalConfig",
    "Resume",
    "ResumeProfile",
    "ResumeSkill",
    "ResumeText",
    "User",
]
