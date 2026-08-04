"""The candidate side of a match: what the resume actually establishes.

A plain value object, built once per resume and reused across every job it is
scored against. Keeping it separate from the ORM means the scoring rules are
testable without a database, and means one resume's profile can never be
accidentally mutated while scoring another's.

Nothing is inferred here. A resume that does not state its years of experience
gets ``years_experience = None``, and the experience rules treat that as
unknown rather than as zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.analysis import taxonomy
from app.analysis.text import normalize_title, normalize_token
from app.core.constants import SENIORITY_RANK, Seniority

if TYPE_CHECKING:
    from app.models.profile import ResumeProfile, ResumeSkill


@dataclass(frozen=True, slots=True)
class ResumeMatchProfile:
    """Everything scoring needs to know about one candidate."""

    resume_id: object
    #: Taxonomy role key. The single target role, corrected value winning.
    target_role_key: str | None
    target_role_display: str | None
    domain_key: str | None
    seniority: Seniority | None
    years_experience: float | None

    #: Taxonomy skill keys the resume demonstrates.
    skill_keys: frozenset[str] = field(default_factory=frozenset)
    #: Display names, for explanations the user reads.
    skill_display: dict[str, str] = field(default_factory=dict)

    #: Same-role search titles, normalised for comparison.
    role_variation_keys: frozenset[str] = field(default_factory=frozenset)
    #: Free-text education lines, lower-cased.
    education: tuple[str, ...] = ()
    certifications: tuple[str, ...] = ()
    responsibilities: tuple[str, ...] = ()

    #: True when the user confirmed or corrected the detected role.
    role_is_confirmed: bool = False

    @property
    def seniority_rank(self) -> int | None:
        return SENIORITY_RANK[self.seniority] if self.seniority else None

    @property
    def role_spec(self) -> taxonomy.RoleSpec | None:
        return taxonomy.get_role(self.target_role_key) if self.target_role_key else None

    @property
    def is_scoreable(self) -> bool:
        """False when the resume has no target role to match against.

        Without one there is nothing to gate on, and every job would pass the
        role check by default - which is exactly the failure mode CLAUDE.md
        section 3 exists to prevent.
        """
        return self.target_role_key is not None

    def has_skill(self, skill_key: str) -> bool:
        return skill_key in self.skill_keys

    def display_for(self, skill_key: str) -> str:
        spec = taxonomy.get_skill(skill_key)
        return self.skill_display.get(skill_key) or (
            spec.display if spec else skill_key
        )


def build_match_profile(
    profile: ResumeProfile, skills: list[ResumeSkill]
) -> ResumeMatchProfile:
    """Build the candidate side from a stored profile and its skills.

    Uses ``effective_*`` throughout, so a user correction takes effect on the
    next rescore without the detected values being overwritten - which is what
    makes "correct the role, then rescore" work (CLAUDE.md section 3).
    """
    role_key = _resolve_role_key(profile.effective_target_role)
    role_spec = taxonomy.get_role(role_key) if role_key else None

    variation_keys = {
        normalize_title(variation)
        for variation in (profile.role_variations or [])
        if isinstance(variation, str) and variation.strip()
    }
    if role_spec is not None:
        # The taxonomy's own variations are authoritative even if the stored
        # profile predates a taxonomy change.
        variation_keys.update(normalize_title(v) for v in role_spec.variations)
        variation_keys.update(role_spec.aliases)
        variation_keys.add(normalize_title(role_spec.canonical_title))

    return ResumeMatchProfile(
        resume_id=profile.resume_id,
        target_role_key=role_key,
        target_role_display=(
            role_spec.canonical_title if role_spec else profile.effective_target_role
        ),
        domain_key=_resolve_domain_key(profile.effective_domain),
        seniority=profile.seniority,
        years_experience=profile.years_experience,
        skill_keys=frozenset(skill.skill_key for skill in skills),
        skill_display={skill.skill_key: skill.display for skill in skills},
        role_variation_keys=frozenset(key for key in variation_keys if key),
        education=_text_tuple(profile.education),
        certifications=_text_tuple(profile.certifications),
        responsibilities=_text_tuple(profile.responsibilities),
        role_is_confirmed=profile.is_confirmed,
    )


def _resolve_role_key(value: str | None) -> str | None:
    """Accept either a taxonomy key or a human-typed title."""
    if not value:
        return None
    if value in taxonomy.ROLES:
        return value
    return taxonomy.role_key_for_term(value)


def _resolve_domain_key(value: str | None) -> str | None:
    if not value:
        return None
    if value in taxonomy.DOMAINS:
        return value
    return taxonomy.domain_key_for_keyword(value)


def _text_tuple(values: list | None) -> tuple[str, ...]:
    """Flatten a JSONB list of strings or dicts into comparable lower-case text."""
    if not values:
        return ()

    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = " ".join(
                str(part)
                for key in ("raw", "text", "value", "name", "display", "title")
                if (part := item.get(key))
            )
        else:
            continue

        normalized = normalize_token(text)
        if normalized:
            result.append(normalized)

    return tuple(result)
