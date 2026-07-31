"""Shared domain vocabulary.

These enums are the single source of truth for values that appear in the
database, the API surface and the frontend. ``GET /api/v1/config`` serves them
to the React client so the dashboard never hardcodes its own copies.
"""

from __future__ import annotations

from enum import StrEnum


class Environment(StrEnum):
    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class ResumeStatus(StrEnum):
    """Lifecycle of an uploaded resume file.

    Phase 1 only ever writes UPLOADED; parsing arrives in Phase 2.
    """

    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSED = "parsed"
    FAILED = "failed"


class ApplicationStatus(StrEnum):
    """Manual, user-controlled application state (CLAUDE.md section 10).

    Deliberately contains no auto-apply concepts: no Easy Apply, no apply
    priority, no form progress, no auto-submit result.

    Note that ``EXPIRED`` is a *job* property (``jobs.expired_at``), not an
    application outcome, so it is not part of this enum. A listing going stale
    must never overwrite a user's APPLIED state. See docs/FRONTEND_INTEGRATION.md.
    """

    NOT_APPLIED = "not_applied"
    SAVED = "saved"
    APPLIED = "applied"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    SKIPPED = "skipped"


class Seniority(StrEnum):
    """Candidate seniority, ordered from least to most senior.

    Used for the experience-compatibility gate in Phase 6.
    """

    INTERN = "intern"
    ENTRY = "entry"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    PRINCIPAL = "principal"
    MANAGER = "manager"
    DIRECTOR = "director"
    EXECUTIVE = "executive"


# Rank order for comparisons. Management levels sit alongside IC levels rather
# than strictly above them, so a Manager is not treated as a senior IC.
SENIORITY_RANK: dict[Seniority, int] = {
    Seniority.INTERN: 0,
    Seniority.ENTRY: 1,
    Seniority.JUNIOR: 2,
    Seniority.MID: 3,
    Seniority.SENIOR: 4,
    Seniority.LEAD: 5,
    Seniority.PRINCIPAL: 6,
    Seniority.MANAGER: 5,
    Seniority.DIRECTOR: 7,
    Seniority.EXECUTIVE: 8,
}


class SkillKind(StrEnum):
    """How an extracted capability is classified."""

    SKILL = "skill"
    TECHNOLOGY = "technology"
    TOOL = "tool"


class ResumeSection(StrEnum):
    """Recognised resume sections used to weight extraction evidence."""

    SUMMARY = "summary"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    SKILLS = "skills"
    CERTIFICATIONS = "certifications"
    PROJECTS = "projects"
    ACHIEVEMENTS = "achievements"
    OTHER = "other"


class FreshnessWindow(StrEnum):
    """Progressive search windows (CLAUDE.md section 6).

    A portal starts at LAST_24H and widens only when the current window has not
    produced enough *qualified* jobs to meet that portal's quota. Age alone is
    never a rejection reason.
    """

    LAST_24H = "last_24h"
    LAST_3D = "last_3d"
    LAST_7D = "last_7d"
    LAST_14D = "last_14d"
    LAST_30D = "last_30d"
    OLDER_ACTIVE = "older_active"


# Ordered widest-last. Iterating this list *is* the escalation strategy.
FRESHNESS_ESCALATION: tuple[FreshnessWindow, ...] = (
    FreshnessWindow.LAST_24H,
    FreshnessWindow.LAST_3D,
    FreshnessWindow.LAST_7D,
    FreshnessWindow.LAST_14D,
    FreshnessWindow.LAST_30D,
    FreshnessWindow.OLDER_ACTIVE,
)


class SortOption(StrEnum):
    """Result ordering (CLAUDE.md section 8). BEST_OVERALL is the default."""

    BEST_OVERALL = "best_overall"
    HIGHEST_MATCH = "highest_match"
    NEWEST_FIRST = "newest_first"
    PORTAL = "portal"
    COMPANY = "company"
    STATUS = "status"


class SearchRunStatus(StrEnum):
    """A run in which some portals failed is COMPLETED_WITH_ERRORS, not FAILED.

    One portal failure must never fail the whole search (CLAUDE.md section 18).
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Allowed values for `requested_limit` on a search run (CLAUDE.md section 5).
RESULT_LIMIT_OPTIONS: tuple[int, ...] = (20, 50, 100)

# Resume upload formats (CLAUDE.md section 2).
SUPPORTED_RESUME_CONTENT_TYPES: tuple[str, ...] = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
)
SUPPORTED_RESUME_EXTENSIONS: tuple[str, ...] = (".pdf", ".docx", ".txt")
