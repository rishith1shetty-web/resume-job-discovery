"""Skill, technology and tool extraction.

Matching is taxonomy-driven and deterministic: only terms registered in
``skills_data`` are recognised. Nothing is inferred - a skill the resume does
not mention is never added (CLAUDE.md: do not infer unsupported skills).
"""

from __future__ import annotations

from app.analysis import taxonomy
from app.analysis.results import Evidence, ExtractedSkill, Section
from app.analysis.text import snippet
from app.core.constants import ResumeSection, SkillKind

# How much a mention counts for, by where it appears. A skill demonstrated in
# the experience section is stronger evidence than one in a keyword list.
SECTION_WEIGHTS: dict[ResumeSection, float] = {
    ResumeSection.EXPERIENCE: 1.0,
    ResumeSection.SKILLS: 0.95,
    ResumeSection.SUMMARY: 0.8,
    ResumeSection.PROJECTS: 0.75,
    ResumeSection.CERTIFICATIONS: 0.6,
    ResumeSection.ACHIEVEMENTS: 0.6,
    ResumeSection.EDUCATION: 0.5,
    ResumeSection.OTHER: 0.4,
}

_MAX_EVIDENCE_PER_SKILL = 3


def extract_skills(sections: list[Section]) -> list[ExtractedSkill]:
    """Find every taxonomy skill mentioned anywhere in the resume.

    Returned in descending confidence order.
    """
    pattern = taxonomy.skill_pattern()
    found: dict[str, ExtractedSkill] = {}

    for section in sections:
        weight = SECTION_WEIGHTS.get(section.kind, 0.4)
        text = section.text
        if not text:
            continue

        for match in pattern.finditer(text):
            key = taxonomy.skill_key_for_term(match.group(0))
            if key is None:
                continue

            spec = taxonomy.SKILLS[key]
            skill = found.get(key)
            if skill is None:
                skill = ExtractedSkill(
                    key=spec.key, display=spec.display, kind=spec.kind
                )
                found[key] = skill

            skill.occurrences += 1
            skill.sections.add(section.kind)

            if len(skill.evidence) < _MAX_EVIDENCE_PER_SKILL:
                # Line number is relative to the section, offset to the document.
                line_offset = text.count("\n", 0, match.start())
                skill.evidence.append(
                    Evidence(
                        kind="skill_match",
                        value=match.group(0),
                        section=section.kind,
                        line_number=section.start_line + line_offset + 1,
                        snippet=snippet(text, match.start(), match.end()),
                        weight=weight,
                    )
                )

    for skill in found.values():
        skill.confidence = _confidence(skill)

    return sorted(
        found.values(),
        key=lambda item: (-item.confidence, -item.occurrences, item.key),
    )


def _confidence(skill: ExtractedSkill) -> float:
    """Confidence that the candidate genuinely has this skill.

    Driven by the strongest section it appeared in, with a small bonus for
    repetition and for appearing in more than one section.
    """
    best_section = max(
        (SECTION_WEIGHTS.get(section, 0.4) for section in skill.sections),
        default=0.4,
    )
    repetition_bonus = min(0.1, 0.025 * (skill.occurrences - 1))
    breadth_bonus = 0.05 if len(skill.sections) > 1 else 0.0
    return round(min(1.0, best_section + repetition_bonus + breadth_bonus), 4)


def skill_keys(skills: list[ExtractedSkill]) -> set[str]:
    return {skill.key for skill in skills}


def partition_by_kind(
    skills: list[ExtractedSkill],
) -> dict[SkillKind, list[ExtractedSkill]]:
    """Group skills into the skill/technology/tool buckets the profile needs."""
    buckets: dict[SkillKind, list[ExtractedSkill]] = {
        SkillKind.SKILL: [],
        SkillKind.TECHNOLOGY: [],
        SkillKind.TOOL: [],
    }
    for skill in skills:
        buckets[skill.kind].append(skill)
    return buckets
