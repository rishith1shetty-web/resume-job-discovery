"""The role/title gate: is this even the right job?

This runs **before** any skill scoring, and its rejections are final. A high
generic skill overlap must never override an incompatible role (CLAUDE.md
section 3, Phase 6 brief): a Financial Analyst and an Accountant share Excel,
SQL and financial statements, and that is exactly why skill overlap cannot be
allowed a vote here.

The checks run in this order, and the first one to decide wins:

1. **Leadership** - a posting whose title carries manager, director,
   principal, lead or executive responsibility the candidate is not near.
2. **Exact role** - the seniority-stripped title resolves to a taxonomy role.
   The target role passes; any other role is rejected outright, whatever else
   the posting says.
3. **Approved variation** - the title matches one of the resume's own
   same-role search titles.
4. **Foreign role phrase** - the title contains another profession's name.
   Checked before the fuzzy tiers so nothing can slip through them.
5. **Strong variant** - the title contains the role title as a phrase, e.g.
   "Financial Analyst, Service Lines".
6. **Weak variant** - every word of the role title appears, and the head noun
   matches, e.g. "Financial Revenue Analyst".
7. Anything else is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.analysis import taxonomy
from app.analysis.text import normalize_title
from app.core.constants import SENIORITY_RANK, Seniority
from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import JobRequirements, strip_seniority

#: Titles at these levels carry responsibility a candidate has to be near.
LEADERSHIP_LEVELS: frozenset[Seniority] = frozenset(
    {
        Seniority.LEAD,
        Seniority.PRINCIPAL,
        Seniority.MANAGER,
        Seniority.DIRECTOR,
        Seniority.EXECUTIVE,
    }
)

#: How far below a leadership title a candidate may sit before it is rejected.
#: One rank is a stretch worth showing; two is a different job.
MAX_LEADERSHIP_GAP = 1


class RoleVerdict(StrEnum):
    """How the job's title relates to the resume's target role."""

    EXACT = "exact"
    VARIATION = "variation"
    STRONG_VARIANT = "strong_variant"
    WEAK_VARIANT = "weak_variant"
    INCOMPATIBLE = "incompatible"


#: Fraction of the role component each verdict earns. A weak variant is still
#: the right family, just less certainly the same job.
ROLE_VERDICT_FACTOR: dict[RoleVerdict, float] = {
    RoleVerdict.EXACT: 1.0,
    RoleVerdict.VARIATION: 1.0,
    RoleVerdict.STRONG_VARIANT: 0.9,
    RoleVerdict.WEAK_VARIANT: 0.75,
    RoleVerdict.INCOMPATIBLE: 0.0,
}


class RoleRejection(StrEnum):
    """Why a posting failed the gate. Stored on the match record."""

    NO_TARGET_ROLE = "no_target_role"
    ROLE_FAMILY_MISMATCH = "role_family_mismatch"
    ROLE_INCOMPATIBLE = "role_incompatible"
    LEADERSHIP_MISMATCH = "leadership_responsibility_mismatch"


@dataclass(frozen=True, slots=True)
class RoleAssessment:
    """The gate's decision, with the evidence that produced it."""

    verdict: RoleVerdict
    factor: float
    reason: str
    detected_role_key: str | None = None
    rejection: RoleRejection | None = None

    @property
    def passed(self) -> bool:
        return self.verdict is not RoleVerdict.INCOMPATIBLE


def evaluate_role(
    profile: ResumeMatchProfile, job: JobRequirements
) -> RoleAssessment:
    """Decide whether this posting is the candidate's role at all."""
    if not profile.is_scoreable:
        return RoleAssessment(
            verdict=RoleVerdict.INCOMPATIBLE,
            factor=0.0,
            reason=(
                "The resume has no detected target role, so no job can be "
                "matched against it."
            ),
            rejection=RoleRejection.NO_TARGET_ROLE,
        )

    role_spec = profile.role_spec
    role_phrase = job.role_phrase
    detected = taxonomy.role_key_for_term(role_phrase)

    # A title that names another profession is reported as that, before the
    # leadership check: "Staff Accountant" is rejected for being an accountant,
    # not for its seniority, and the reason the user reads should say so.
    if detected is not None and detected != profile.target_role_key:
        other = taxonomy.get_role(detected)
        return RoleAssessment(
            verdict=RoleVerdict.INCOMPATIBLE,
            factor=0.0,
            reason=(
                f"Title is {other.canonical_title if other else detected}, a "
                f"different role from {profile.target_role_display}. Shared "
                "skills do not make it the same job."
            ),
            detected_role_key=detected,
            rejection=RoleRejection.ROLE_FAMILY_MISMATCH,
        )

    leadership = _leadership_rejection(profile, job)
    if leadership is not None:
        return leadership

    if detected is not None:
        return RoleAssessment(
            verdict=RoleVerdict.EXACT,
            factor=ROLE_VERDICT_FACTOR[RoleVerdict.EXACT],
            reason=f"Title is the target role ({profile.target_role_display}).",
            detected_role_key=detected,
        )

    # 3. One of the resume's approved same-role search titles.
    if role_phrase in profile.role_variation_keys:
        return RoleAssessment(
            verdict=RoleVerdict.VARIATION,
            factor=ROLE_VERDICT_FACTOR[RoleVerdict.VARIATION],
            reason=f"Title is an approved variation of {profile.target_role_display}.",
            detected_role_key=profile.target_role_key,
        )

    # 4. Another profession named inside a longer title.
    foreign = _foreign_role_phrase(role_phrase, profile.target_role_key)
    if foreign is not None:
        other = taxonomy.get_role(foreign)
        return RoleAssessment(
            verdict=RoleVerdict.INCOMPATIBLE,
            factor=0.0,
            reason=(
                f"Title names {other.canonical_title if other else foreign}, a "
                f"different role from {profile.target_role_display}."
            ),
            detected_role_key=foreign,
            rejection=RoleRejection.ROLE_FAMILY_MISMATCH,
        )

    if role_spec is None:  # pragma: no cover - is_scoreable implies a spec
        return RoleAssessment(
            verdict=RoleVerdict.INCOMPATIBLE,
            factor=0.0,
            reason="The resume's target role is not in the taxonomy.",
            rejection=RoleRejection.NO_TARGET_ROLE,
        )

    # 5. The role title appears as a phrase inside a longer title.
    for phrase in _target_phrases(role_spec, profile):
        if _contains_phrase(role_phrase, phrase):
            return RoleAssessment(
                verdict=RoleVerdict.STRONG_VARIANT,
                factor=ROLE_VERDICT_FACTOR[RoleVerdict.STRONG_VARIANT],
                reason=(
                    f"Title contains {phrase!r}, a qualified form of "
                    f"{profile.target_role_display}."
                ),
                detected_role_key=profile.target_role_key,
            )

    # 6. Every word of the role title is present and the head noun matches.
    canonical_tokens = set(normalize_title(role_spec.canonical_title).split())
    job_tokens = role_phrase.split()
    head_matches = bool(job_tokens) and job_tokens[-1] == _head_noun(role_spec)

    if head_matches and canonical_tokens <= set(job_tokens):
        return RoleAssessment(
            verdict=RoleVerdict.WEAK_VARIANT,
            factor=ROLE_VERDICT_FACTOR[RoleVerdict.WEAK_VARIANT],
            reason=(
                f"Title is a qualified {profile.target_role_display} "
                "with an extra specialisation."
            ),
            detected_role_key=profile.target_role_key,
        )

    return RoleAssessment(
        verdict=RoleVerdict.INCOMPATIBLE,
        factor=0.0,
        reason=(
            f"Title {job.title!r} is not {profile.target_role_display} or an "
            "approved variation of it."
        ),
        rejection=RoleRejection.ROLE_INCOMPATIBLE,
    )


def _leadership_rejection(
    profile: ResumeMatchProfile, job: JobRequirements
) -> RoleAssessment | None:
    """Reject a leadership posting the candidate is not close to.

    Only rejects when the resume states a seniority. Without one there is no
    basis for the comparison, and inventing one would reject jobs on a guess.
    """
    job_level = job.title_seniority
    if job_level is None or job_level not in LEADERSHIP_LEVELS:
        return None

    candidate_rank = profile.seniority_rank
    if candidate_rank is None:
        return None

    gap = SENIORITY_RANK[job_level] - candidate_rank
    if gap <= MAX_LEADERSHIP_GAP:
        return None

    return RoleAssessment(
        verdict=RoleVerdict.INCOMPATIBLE,
        factor=0.0,
        reason=(
            f"Title carries {job_level.value} responsibility, "
            f"{gap} levels above the resume's {profile.seniority.value if profile.seniority else 'unknown'} level."
        ),
        rejection=RoleRejection.LEADERSHIP_MISMATCH,
    )


def _target_phrases(
    role_spec: taxonomy.RoleSpec, profile: ResumeMatchProfile
) -> tuple[str, ...]:
    """Phrases whose presence identifies the target role, longest first."""
    phrases = {
        normalize_title(role_spec.canonical_title),
        *(normalize_title(v) for v in role_spec.variations),
        *role_spec.aliases,
        *profile.role_variation_keys,
    }

    return tuple(sorted((p for p in phrases if p), key=len, reverse=True))


def _foreign_role_phrase(role_phrase: str, target_key: str | None) -> str | None:
    """The key of another role named inside this title, if any.

    Only whole-phrase matches count, so "Business Financial Analyst" does not
    register "business analyst" - those words are not adjacent.
    """
    for key, role in taxonomy.ROLES.items():
        if key == target_key:
            continue

        for term in (normalize_title(role.canonical_title), *role.aliases):
            if term and _contains_phrase(role_phrase, term):
                return key

    return None


def _contains_phrase(haystack: str, needle: str) -> bool:
    """Whole-token phrase containment, so "analyst" never matches "analysts"."""
    if not needle:
        return False

    haystack_tokens = haystack.split()
    needle_tokens = needle.split()
    if len(needle_tokens) > len(haystack_tokens):
        return False

    for start in range(len(haystack_tokens) - len(needle_tokens) + 1):
        if haystack_tokens[start : start + len(needle_tokens)] == needle_tokens:
            return True

    return False


def _head_noun(role_spec: taxonomy.RoleSpec) -> str:
    tokens = normalize_title(role_spec.canonical_title).split()
    return tokens[-1] if tokens else ""
