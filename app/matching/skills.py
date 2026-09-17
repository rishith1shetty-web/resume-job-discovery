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

# Why a shortfall of only generic skills is floored

Coverage is a plain fraction, so every stated requirement weighs the same
whether it is "Kubernetes" or "Communication". That produced a cliff running
the wrong way: a posting naming NO skill scored the neutral 0.7, while one
naming a single generic skill the resume happens not to list scored 0.0 - a
21-point difference turning on whether the employer wrote one recognisable
word. Measured over 462 live postings it hit 72 of them, and the jobs it hit
were not weak matches but exact-role ones with full experience credit.

So a shortfall made up *entirely* of generic skills is floored back to the
same neutral credit an unspecific posting already receives - no better, no
worse. The floor never applies when any missing requirement is a real one, and
never lifts coverage above neutral, so a missing SQL still costs exactly what
it costs today.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.analysis import taxonomy
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

    # A shortfall of nothing but generic skills says no more about the fit than
    # an advert that listed no skills at all, so it earns the same neutral
    # credit and never more. One real missing skill disables this entirely.
    missing_required_keys = {
        requirement.skill_key
        for requirement in required
        if not profile.has_skill(requirement.skill_key)
    }
    if (
        missing_required_keys
        and missing_required_keys <= GENERIC_SKILL_KEYS
        and required_coverage < _NEUTRAL_COVERAGE
    ):
        required_coverage = _NEUTRAL_COVERAGE

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

#: Skills that identify no field of work. Derived from the taxonomy rather than
#: listed here, so a skill added to ``skills_data.py`` is classified by the
#: domains it declares and this set cannot drift out of step with it.
#:
#: As it stands the taxonomy leaves exactly nine skills domain-less, and every
#: one is a soft skill or a generic office tool: Communication, Leadership,
#: Microsoft Office, PowerPoint, Presentation Skills, Problem Solving,
#: Teamwork, Time Management and Microsoft Word. Every technical and
#: domain skill checked - SQL, Python, GAAP, Cisco, AWS, Workday, Power BI,
#: Excel, Tableau, TCP/IP and the rest - declares at least one domain and is
#: therefore untouched by the floor above.
GENERIC_SKILL_KEYS: frozenset[str] = frozenset(
    spec.key for spec in taxonomy.SKILLS.values() if not spec.domains
)


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
