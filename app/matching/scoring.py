"""The score itself: weights, hard rejections, and the qualification decision.

# Components

One place defines what a match is worth. The weights total exactly 100 and are
asserted to at import, so a future edit cannot silently produce scores that no
longer mean percentages.

| Component              | Weight | What it measures                          |
|------------------------|--------|-------------------------------------------|
| Role / title           |     30 | Is this the candidate's job at all        |
| Required skills        |     30 | Coverage of stated must-haves             |
| Preferred skills       |     15 | Coverage of nice-to-haves                 |
| Experience & seniority |     15 | Years and level fit                       |
| Domain, education, cert|      5 | Field and credential relevance            |
| Supporting evidence    |      5 | Responsibility overlap, description depth |

# Hard rejections

A rejection is final and the score is reported as-is - never nudged up to the
threshold, and never nudged down to justify the outcome. Each carries a code:

``no_target_role``          the resume has no role to match against
``role_family_mismatch``    the title is a different profession
``role_incompatible``       the title is not the target role or a variation
``leadership_responsibility_mismatch``
                            the title carries leadership the candidate is not near
``insufficient_description``the posting cannot be scored reliably
``missing_mandatory_skill`` a stated must-have skill is absent
``missing_mandatory_certification``
                            a stated must-have certification is absent
``insufficient_education``  a mandatory degree level is not evidenced
``missing_security_clearance``
                            a clearance the posting requires is not evidenced
``insufficient_experience`` years fall far short of a stated minimum
``below_threshold``         no hard rule failed, but the score is under 70

# Rounding

The total is rounded half-up to two decimals, then compared to the threshold.
Half-up rather than Python's default banker's rounding, so 69.995 becomes
70.00 and the boundary behaves the way a reader expects. A score of exactly
70.00 qualifies; 69.99 does not (CLAUDE.md section 18: the threshold is locked
and never lowered to fill a result count).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from app.core.constants import MIN_MATCH_SCORE_FLOOR
from app.matching.experience import ExperienceAssessment, evaluate_experience
from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import (
    JobRequirements,
    resume_degree_level,
    resume_has_certification,
)
from app.matching.roles import RoleAssessment, RoleRejection, evaluate_role
from app.matching.skills import SkillAssessment, evaluate_skills
from app.matching.version import SCORING_VERSION

# --- Weights ----------------------------------------------------------------

WEIGHT_ROLE = 30.0
WEIGHT_REQUIRED_SKILLS = 30.0
WEIGHT_PREFERRED_SKILLS = 15.0
WEIGHT_EXPERIENCE = 15.0
WEIGHT_DOMAIN_EDUCATION = 5.0
WEIGHT_SUPPORTING = 5.0

COMPONENT_WEIGHTS: dict[str, float] = {
    "role": WEIGHT_ROLE,
    "required_skills": WEIGHT_REQUIRED_SKILLS,
    "preferred_skills": WEIGHT_PREFERRED_SKILLS,
    "experience": WEIGHT_EXPERIENCE,
    "domain_education": WEIGHT_DOMAIN_EDUCATION,
    "supporting": WEIGHT_SUPPORTING,
}

MAX_SCORE = 100.0
# A defect here would silently stop scores being percentages.
assert sum(COMPONENT_WEIGHTS.values()) == MAX_SCORE, "component weights must total 100"

#: The locked threshold. Raising it is allowed; lowering it is not.
QUALIFICATION_THRESHOLD = float(MIN_MATCH_SCORE_FLOOR)


class RejectionCode(StrEnum):
    """Why a job did not qualify. Stored on the match record."""

    NO_TARGET_ROLE = "no_target_role"
    ROLE_FAMILY_MISMATCH = "role_family_mismatch"
    ROLE_INCOMPATIBLE = "role_incompatible"
    LEADERSHIP_MISMATCH = "leadership_responsibility_mismatch"
    INSUFFICIENT_DESCRIPTION = "insufficient_description"
    MISSING_MANDATORY_SKILL = "missing_mandatory_skill"
    MISSING_MANDATORY_CERTIFICATION = "missing_mandatory_certification"
    INSUFFICIENT_EDUCATION = "insufficient_education"
    MISSING_SECURITY_CLEARANCE = "missing_security_clearance"
    INSUFFICIENT_EXPERIENCE = "insufficient_experience"
    BELOW_THRESHOLD = "below_threshold"


@dataclass(frozen=True, slots=True)
class MatchResult:
    """One resume scored against one job, with the reasoning intact."""

    final_score: float
    qualified: bool
    scoring_version: str

    role: RoleAssessment
    skills: SkillAssessment
    experience: ExperienceAssessment

    component_scores: dict[str, float] = field(default_factory=dict)
    positive_evidence: tuple[str, ...] = ()
    penalties: tuple[str, ...] = ()
    rejection_code: RejectionCode | None = None
    rejection_reason: str | None = None

    domain_summary: str = ""
    education_summary: str = ""

    @property
    def component_score(self) -> dict[str, float]:
        return dict(self.component_scores)

    def to_details(self) -> dict[str, Any]:
        """Serialisable explanation, stored on ``job_matches.score_details``."""
        return {
            "scoring_version": self.scoring_version,
            "threshold": QUALIFICATION_THRESHOLD,
            "components": {
                name: {
                    "score": round(self.component_scores.get(name, 0.0), 2),
                    "weight": weight,
                }
                for name, weight in COMPONENT_WEIGHTS.items()
            },
            "role": {
                "verdict": self.role.verdict.value,
                "reason": self.role.reason,
                "detected_role_key": self.role.detected_role_key,
            },
            "skills": {
                "matched_required": list(self.skills.matched_required),
                "missing_required": list(self.skills.missing_required),
                "matched_preferred": list(self.skills.matched_preferred),
                "missing_mandatory": list(self.skills.missing_mandatory),
                "required_coverage": round(self.skills.required_coverage, 3),
                "preferred_coverage": round(self.skills.preferred_coverage, 3),
            },
            "experience": {
                "summary": self.experience.summary,
                "years_factor": round(self.experience.years_factor, 3),
                "seniority_factor": round(self.experience.seniority_factor, 3),
            },
            "domain": self.domain_summary,
            "education": self.education_summary,
            "positive_evidence": list(self.positive_evidence),
            "penalties": list(self.penalties),
        }


def round_score(value: float) -> float:
    """Round half-up to two decimals.

    Not Python's ``round``: that rounds halves to even, so 70.005 would become
    70.0 and 69.995 would become 70.0 too. Half-up is what a reader expects at
    a published threshold.
    """
    return float(
        Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def score_match(profile: ResumeMatchProfile, job: JobRequirements) -> MatchResult:
    """Score one job for one resume. Pure, deterministic, no clock, no network.

    Rejections are computed first and the score is still reported, so a
    rejected job can be explained rather than just refused.
    """
    role = evaluate_role(profile, job)
    skills = evaluate_skills(profile, job)
    experience = evaluate_experience(profile, job)

    components = {
        "role": WEIGHT_ROLE * role.factor,
        "required_skills": WEIGHT_REQUIRED_SKILLS * skills.required_coverage,
        "preferred_skills": WEIGHT_PREFERRED_SKILLS * skills.preferred_coverage,
        "experience": WEIGHT_EXPERIENCE * experience.factor,
    }

    domain_factor, domain_summary, domain_notes = _domain_factor(profile, job)
    education_factor, education_summary, education_notes = _education_factor(
        profile, job
    )
    components["domain_education"] = WEIGHT_DOMAIN_EDUCATION * round(
        0.5 * domain_factor + 0.5 * education_factor, 6
    )

    supporting_factor, supporting_notes = _supporting_factor(profile, job)
    components["supporting"] = WEIGHT_SUPPORTING * supporting_factor

    total = round_score(min(MAX_SCORE, max(0.0, sum(components.values()))))

    positive: list[str] = []
    penalties: list[str] = []

    if role.passed:
        positive.append(role.reason)
    else:
        penalties.append(role.reason)

    positive.extend(skills.evidence)
    positive.extend(note for note in experience.evidence if note)
    positive.extend(domain_notes)
    positive.extend(education_notes)
    positive.extend(supporting_notes)
    penalties.extend(skills.penalties)
    penalties.extend(experience.penalties)

    rejection = _first_rejection(role, skills, experience, job, profile)

    if rejection is None and total < QUALIFICATION_THRESHOLD:
        rejection = (
            RejectionCode.BELOW_THRESHOLD,
            (
                f"Scored {total:.2f}, below the locked {QUALIFICATION_THRESHOLD:.0f}% "
                "threshold."
            ),
        )

    if rejection is not None:
        penalties.append(rejection[1])

    return MatchResult(
        final_score=total,
        qualified=rejection is None,
        scoring_version=SCORING_VERSION,
        role=role,
        skills=skills,
        experience=experience,
        component_scores={name: round(value, 4) for name, value in components.items()},
        positive_evidence=tuple(dict.fromkeys(note for note in positive if note)),
        penalties=tuple(dict.fromkeys(note for note in penalties if note)),
        rejection_code=rejection[0] if rejection else None,
        rejection_reason=rejection[1] if rejection else None,
        domain_summary=domain_summary,
        education_summary=education_summary,
    )


_ROLE_REJECTION_CODES: dict[RoleRejection, RejectionCode] = {
    RoleRejection.NO_TARGET_ROLE: RejectionCode.NO_TARGET_ROLE,
    RoleRejection.ROLE_FAMILY_MISMATCH: RejectionCode.ROLE_FAMILY_MISMATCH,
    RoleRejection.ROLE_INCOMPATIBLE: RejectionCode.ROLE_INCOMPATIBLE,
    RoleRejection.LEADERSHIP_MISMATCH: RejectionCode.LEADERSHIP_MISMATCH,
}


def _first_rejection(
    role: RoleAssessment,
    skills: SkillAssessment,
    experience: ExperienceAssessment,
    job: JobRequirements,
    profile: ResumeMatchProfile,
) -> tuple[RejectionCode, str] | None:
    """The first hard rule this job fails, in order of decisiveness."""
    if role.rejection is not None:
        return _ROLE_REJECTION_CODES[role.rejection], role.reason

    if not job.has_scoreable_description:
        return (
            RejectionCode.INSUFFICIENT_DESCRIPTION,
            (
                f"The posting's description is {job.description_chars} characters, "
                "too little to judge the match reliably."
            ),
        )

    if skills.has_missing_mandatory:
        missing = ", ".join(skills.missing_mandatory)
        return (
            RejectionCode.MISSING_MANDATORY_SKILL,
            f"The posting requires {missing}, which the resume does not evidence.",
        )

    missing_certs = [
        name
        for name in job.mandatory_certifications
        if not resume_has_certification(profile.certifications, name)
    ]
    if missing_certs:
        return (
            RejectionCode.MISSING_MANDATORY_CERTIFICATION,
            (
                f"The posting requires {', '.join(c.upper() for c in missing_certs)}, "
                "which the resume does not evidence."
            ),
        )

    if job.degree_is_mandatory and job.required_degree_level is not None:
        candidate_level = resume_degree_level(profile.education)
        if candidate_level is None or candidate_level < job.required_degree_level:
            return (
                RejectionCode.INSUFFICIENT_EDUCATION,
                (
                    "The posting requires a degree level the resume does not "
                    "evidence."
                ),
            )

    if job.requires_clearance and not _resume_mentions_clearance(profile):
        return (
            RejectionCode.MISSING_SECURITY_CLEARANCE,
            (
                "The posting requires a security clearance the resume does not "
                "evidence."
            ),
        )

    if experience.insufficient_experience:
        return (
            RejectionCode.INSUFFICIENT_EXPERIENCE,
            experience.summary,
        )

    return None


def _resume_mentions_clearance(profile: ResumeMatchProfile) -> bool:
    haystack = " ".join(
        (*profile.certifications, *profile.education, *profile.responsibilities)
    )
    return "clearance" in haystack


def _domain_factor(
    profile: ResumeMatchProfile, job: JobRequirements
) -> tuple[float, str, tuple[str, ...]]:
    if profile.domain_key is None:
        return 0.7, "Resume domain not detected.", ()

    if not job.domain_keys:
        return (
            0.8,
            "The posting names no clear domain.",
            (),
        )

    if profile.domain_key in job.domain_keys:
        return (
            1.0,
            f"Posting is in the resume's domain ({profile.domain_key}).",
            (f"Domain matches: {profile.domain_key}.",),
        )

    return (
        0.5,
        (
            f"Posting reads as {', '.join(sorted(job.domain_keys))}, not "
            f"{profile.domain_key}."
        ),
        (),
    )


def _education_factor(
    profile: ResumeMatchProfile, job: JobRequirements
) -> tuple[float, str, tuple[str, ...]]:
    notes: list[str] = []

    matched_certs = [
        name
        for name in job.required_certifications
        if resume_has_certification(profile.certifications, name)
    ]
    if matched_certs:
        notes.append(
            "Holds certifications the posting asks for: "
            + ", ".join(c.upper() for c in matched_certs)
        )

    if job.required_degree_level is None:
        summary = "The posting states no education requirement."
        # Nothing to fall short of; a stated certification match still helps.
        return (1.0 if not job.required_certifications else 0.9 + 0.1 * bool(matched_certs)), summary, tuple(notes)

    candidate_level = resume_degree_level(profile.education)
    if candidate_level is None:
        return (
            0.6,
            "The posting states a degree requirement the resume does not evidence.",
            tuple(notes),
        )

    if candidate_level >= job.required_degree_level:
        notes.append("Education meets the posting's requirement.")
        return 1.0, "Education requirement met.", tuple(notes)

    return (
        0.5,
        "The resume's highest degree is below the posting's requirement.",
        tuple(notes),
    )


def _supporting_factor(
    profile: ResumeMatchProfile, job: JobRequirements
) -> tuple[float, tuple[str, ...]]:
    """Corroboration from the description beyond its stated requirements.

    Deliberately small and deliberately not keyword overlap over the whole
    text: CLAUDE.md section 7 forbids plain keyword overlap as an authoritative
    signal, so this only ever contributes five points.
    """
    if not job.has_scoreable_description:
        return 0.0, ()

    core_skills = set(profile.role_spec.core_skills) if profile.role_spec else set()
    if not core_skills:
        return 0.7, ()

    # Every skill the posting names counts here, including ones it only
    # mentions in passing - corroboration is exactly what those are good for.
    stated = {requirement.skill_key for requirement in job.skills}
    overlap = core_skills & stated & profile.skill_keys

    if not overlap:
        return 0.5, ()

    factor = min(1.0, 0.6 + 0.1 * len(overlap))
    return factor, (
        "Posting asks for core skills of the role that the resume evidences: "
        + ", ".join(sorted(profile.display_for(key) for key in overlap)),
    )
