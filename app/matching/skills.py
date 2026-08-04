"""Skill overlap between a resume and a posting.

Required skills weigh more than preferred ones, because missing a must-have
and missing a nice-to-have are not the same failure.

Aliases come from the taxonomy, which already maps safe surface forms -
"Power BI" and "Microsoft Power BI", "PostgreSQL" and "Postgres", "Excel" and
"Microsoft Excel" - onto one key. Nothing is inferred beyond that: an
unregistered term is not a skill, and a related-but-different tool is not a
substitute for the one asked for.

Only a **mandatory** missing skill can reject a job. A requirement the posting
states ambiguously reduces the score instead, which is the direction the brief
asks for: uncertain requirements should cost points, not opportunities.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.matching.profile import ResumeMatchProfile
from app.matching.requirements import JobRequirements, SkillRequirement


@dataclass(frozen=True, slots=True)
class SkillAssessment:
    """How well the candidate's skills cover what the posting asks for."""

    #: 0.0 to 1.0 coverage of required skills.
    required_coverage: float
    #: 0.0 to 1.0 coverage of preferred skills.
    preferred_coverage: float

    matched_required: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    matched_preferred: tuple[str, ...] = ()
    missing_preferred: tuple[str, ...] = ()
    #: Skills the posting mentions without asking for them. Not scored here;
    #: they corroborate the match in the supporting component.
    matched_supporting: tuple[str, ...] = ()
    #: Missing skills the posting called must-haves. These reject.
    missing_mandatory: tuple[str, ...] = ()

    #: True when the posting named no recognised skills at all.
    no_skills_stated: bool = False
    evidence: tuple[str, ...] = field(default_factory=tuple)
    penalties: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_missing_mandatory(self) -> bool:
        return bool(self.missing_mandatory)


def evaluate_skills(
    profile: ResumeMatchProfile, job: JobRequirements
) -> SkillAssessment:
    """Compare resume skills against the posting's stated requirements."""
    required = job.required_skills
    preferred = job.preferred_skills

    matched_required, missing_required, missing_mandatory = _split(profile, required)
    matched_preferred, missing_preferred, _ = _split(profile, preferred)
    matched_supporting, _, _ = _split(profile, job.supporting_skills)

    # A posting that names no recognised skill gets neutral credit rather than
    # zero. Scoring it as a total miss would punish the candidate for the
    # employer writing a vague advert, and the role gate has already
    # established this is the right job.
    no_skills_stated = not required and not preferred

    required_coverage = (
        len(matched_required) / len(required) if required else _NEUTRAL_COVERAGE
    )
    preferred_coverage = (
        len(matched_preferred) / len(preferred) if preferred else _NEUTRAL_COVERAGE
    )

    evidence: list[str] = []
    penalties: list[str] = []

    if matched_required:
        evidence.append(
            f"Has {len(matched_required)} of {len(required)} required skills: "
            + ", ".join(matched_required[:6])
        )
    if matched_preferred:
        evidence.append("Also has preferred: " + ", ".join(matched_preferred[:6]))
    if matched_supporting:
        evidence.append(
            "Description mentions: " + ", ".join(matched_supporting[:6])
        )
    if no_skills_stated:
        evidence.append(
            "The posting names no recognised skills; scored on role fit and "
            "experience instead."
        )

    if missing_required:
        penalties.append("Missing required skills: " + ", ".join(missing_required[:6]))
    if missing_preferred:
        penalties.append(
            "Missing preferred skills: " + ", ".join(missing_preferred[:6])
        )

    return SkillAssessment(
        required_coverage=required_coverage,
        preferred_coverage=preferred_coverage,
        matched_required=matched_required,
        missing_required=missing_required,
        matched_preferred=matched_preferred,
        missing_preferred=missing_preferred,
        matched_supporting=matched_supporting,
        missing_mandatory=missing_mandatory,
        no_skills_stated=no_skills_stated,
        evidence=tuple(evidence),
        penalties=tuple(penalties),
    )


#: Credit given when a posting states no requirement of a given kind. Not full
#: marks - an unstated requirement is not evidence of a fit - but not zero.
_NEUTRAL_COVERAGE = 0.7


def _split(
    profile: ResumeMatchProfile, requirements: tuple[SkillRequirement, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Partition requirements into matched, missing, and missing-mandatory."""
    matched: list[str] = []
    missing: list[str] = []
    missing_mandatory: list[str] = []

    for requirement in requirements:
        if profile.has_skill(requirement.skill_key):
            matched.append(requirement.display)
            continue

        missing.append(requirement.display)
        if requirement.is_mandatory:
            missing_mandatory.append(requirement.display)

    # Sorted so the same inputs always produce the same explanation text.
    return tuple(sorted(matched)), tuple(sorted(missing)), tuple(sorted(missing_mandatory))
