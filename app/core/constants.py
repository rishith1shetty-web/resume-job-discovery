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
#: How widely to *query*, which is not the same question as what is eligible.
#: OLDER_ACTIVE stays on the ladder because it is the one pass that sends no
#: date filter, and portals' own date filters are coarser than ours - dropping
#: it lost listings that were inside the 30-day window but that the narrower
#: queries never returned. Eligibility is enforced separately, by
#: ``is_eligible_age``, so this final pass cannot admit an over-age job.
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


# A run in one of these states will not change again.
TERMINAL_SEARCH_RUN_STATUSES: frozenset[SearchRunStatus] = frozenset(
    {
        SearchRunStatus.COMPLETED,
        SearchRunStatus.COMPLETED_WITH_ERRORS,
        SearchRunStatus.FAILED,
        SearchRunStatus.CANCELLED,
    }
)


class PortalRunStatus(StrEnum):
    """One portal's leg of a search run.

    SKIPPED covers a portal that was selected but never attempted, e.g. it was
    disabled between run creation and execution. It is distinct from FAILED so
    the run summary can tell "did not run" apart from "tried and broke".
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RawJobStatus(StrEnum):
    """Processing state of a listing exactly as a portal returned it.

    ``raw_jobs`` is an audit trail: rows are never edited into shape, they are
    promoted to a canonical ``jobs`` row and marked here. Phase 5 owns the
    transitions; Phase 3 only defines them.
    """

    COLLECTED = "collected"
    NORMALIZED = "normalized"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    FAILED = "failed"


class JobUrlKind(StrEnum):
    """Where an application URL points (docs/DECISIONS.md D-002, D-003).

    Recorded per source so Phase 5 can pick the most direct usable URL when the
    same vacancy arrives from several portals.
    """

    EMPLOYER_CAREER = "employer_career"
    ATS = "ats"
    PORTAL_LISTING = "portal_listing"
    AGGREGATOR_REDIRECT = "aggregator_redirect"
    UNKNOWN = "unknown"


class CareerPageStatus(StrEnum):
    """How far a company's career page has got through verification.

    Only ``VERIFIED`` is ever shown to a user or made clickable. The other
    members exist so a refusal can be recorded with its cause rather than
    disappearing silently, which is what makes "why is this company missing?"
    answerable.

    ``REJECTED`` and ``UNAVAILABLE`` are deliberately different verdicts about
    different things. Rejected is a statement about the *page*: it is a login
    wall, a dead URL, the vendor's front door, or another employer's site, and
    re-asking tomorrow will not change that. Unavailable is a statement about
    *our request*: the site declined it, timed out, or robots.txt asks us not
    to fetch it, so we simply do not know. Both omit the company; only the
    second is worth retrying soon.
    """

    #: Derived from a real posting, not yet fetched. Never leaves the server.
    DISCOVERED = "discovered"
    #: A fetch is in flight. Transient, and never persisted as a final state.
    VALIDATING = "validating"
    #: Fetched and proven: reachable, public, this employer's, with jobs on it.
    VERIFIED = "verified"
    #: Fetched and disproven. The page is not a usable public career portal.
    REJECTED = "rejected"
    #: We were not allowed to look - declined, blocked, or robots-disallowed.
    UNAVAILABLE = "unavailable"
    #: Fetched, nothing disqualifying found, but no evidence of jobs either.
    #: Not shown: absence of proof is not proof, and the brief is explicit that
    #: omitting a company beats sending a user somewhere unusable.
    UNVERIFIED = "unverified"


# Preference order for the canonical application URL (CLAUDE.md section 9).
# Lower rank wins. Phase 5 implements the resolution; this is the rule it uses.
JOB_URL_PREFERENCE: dict[JobUrlKind, int] = {
    JobUrlKind.EMPLOYER_CAREER: 0,
    JobUrlKind.ATS: 1,
    JobUrlKind.PORTAL_LISTING: 2,
    JobUrlKind.AGGREGATOR_REDIRECT: 3,
    JobUrlKind.UNKNOWN: 4,
}


class ApplicationType(StrEnum):
    """How much of an application the employer actually asks for (Phase 13).

    This product exists to find jobs worth applying to properly. A one-click
    submission against an aggregator profile is a different act from completing
    an employer's own form, and only the second is returned.

    ``UNKNOWN`` is a real state, not a synonym for either: it means no evidence
    either way was found. It resolves to eligible, because refusing every
    listing that failed to advertise its application style would silently
    discard most of the market - but it stays distinct so the reason a job was
    admitted is always recoverable.
    """

    LONG_APPLICATION = "long_application"
    QUICK_APPLY = "quick_apply"
    UNKNOWN = "unknown"

    @property
    def is_eligible(self) -> bool:
        """Whether a job of this type may reach matching.

        The single place the admission rule lives. Only ``QUICK_APPLY`` is
        refused; ``UNKNOWN`` is admitted, and is admitted *because* nothing
        indicated a quick apply, never as a guess that it is a long one.
        """
        return self is not ApplicationType.QUICK_APPLY


class WorkMode(StrEnum):
    """Dashboard "location / work mode" column (CLAUDE.md section 10)."""

    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class PortalErrorKind(StrEnum):
    """Why a portal leg failed.

    Kept coarse and portal-agnostic: the core never branches on a portal name,
    so it must be able to reason about failures generically.
    """

    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTH_REQUIRED = "auth_required"
    BLOCKED = "blocked"
    NETWORK = "network"
    PARSE = "parse"
    NOT_IMPLEMENTED = "not_implemented"
    UNKNOWN = "unknown"


# Allowed values for `requested_limit` on a search run (CLAUDE.md section 5).
RESULT_LIMIT_OPTIONS: tuple[int, ...] = (20, 50, 100)

# CLAUDE.md section 18: the match threshold is locked. It may be raised, never
# lowered to fill a quota. This is the floor both the settings validator and
# the `search_runs.min_score` CHECK constraint enforce.
MIN_MATCH_SCORE_FLOOR: int = 70

# How many portals may be enabled, and therefore selected for one search run
# (CLAUDE.md section 4).
#
# Defined here rather than inline because it is one rule with two enforcement
# points that must not drift: `Settings.max_enabled_portals`, which bounds the
# configured value and is what `GET /config` reports, and the `max_length` on
# `SearchRunCreate.selected_portal_ids`, which is what actually returns the
# 422. They were separate literals and had to be changed in step; now there is
# one number to change.
#
# A ceiling, not a target. Raising it enables nothing: which portals are on is
# `portals.is_enabled` in the database, and a portal is only selectable once an
# adapter is registered for it as well.
MAX_ENABLED_PORTALS: int = 16

# Scores are percentages on a 0-100 scale, matching the 70% threshold.
SCORE_MIN: float = 0.0
SCORE_MAX: float = 100.0

# Resume upload formats (CLAUDE.md section 2).
SUPPORTED_RESUME_CONTENT_TYPES: tuple[str, ...] = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
)
SUPPORTED_RESUME_EXTENSIONS: tuple[str, ...] = (".pdf", ".docx", ".txt")
