"""SQLAlchemy models.

Every model must be imported here: Alembic autogenerate only sees tables that
are attached to ``Base.metadata`` at import time.

Phase 1 defined the foundation (users, resumes, portals, portal_configs).
Phase 2 added resume_texts, resume_profiles and resume_skills.
Phase 3 completes the CLAUDE.md section 12 set: search_runs, search_run_portals,
search_queries, raw_jobs, jobs, job_sources, job_matches, applications,
application_status_history and portal_errors.

Since then: career_page_interactions, which records the Applied/Skipped
decision a user makes about a company's career page. Its own table rather than
a row in ``applications``, because a career page has no ``jobs`` row to
reference - see the model for why that distinction is worth a table.
"""

from app.db.base import Base
from app.models.application import Application, ApplicationStatusHistory
from app.models.career_page import CareerPageInteraction
from app.models.job import Job, JobMatch, JobSource, RawJobRecord
from app.models.portal import Portal, PortalConfig
from app.models.profile import ResumeProfile, ResumeSkill, ResumeText
from app.models.resume import Resume
from app.models.search import PortalError, SearchQuery, SearchRun, SearchRunPortal
from app.models.user import User

__all__ = [
    "Application",
    "ApplicationStatusHistory",
    "Base",
    "CareerPageInteraction",
    "Job",
    "JobMatch",
    "JobSource",
    "Portal",
    "PortalConfig",
    "PortalError",
    "RawJobRecord",
    "Resume",
    "ResumeProfile",
    "ResumeSkill",
    "ResumeText",
    "SearchQuery",
    "SearchRun",
    "SearchRunPortal",
    "User",
]
