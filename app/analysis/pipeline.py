"""Resume analysis pipeline.

Orchestrates deterministic extraction end to end. Pure and synchronous: it
takes text and returns a ``ResumeProfileData``, with no database, HTTP or
network involvement, so the whole analysis is unit-testable and reproducible.

No LLM is involved. See ``app/analysis/llm.py``.
"""

from __future__ import annotations

from datetime import datetime

from app.analysis.detection import detect_domain, detect_target_role
from app.analysis.education import (
    extract_certifications,
    extract_education,
    extract_responsibilities,
)
from app.analysis.experience import compute_years_experience
from app.analysis.results import ResumeProfileData
from app.analysis.sections import split_sections
from app.analysis.seniority import detect_seniority
from app.analysis.skills import extract_skills
from app.analysis.titles import extract_titles
from app.analysis.variations import generate_role_variations
from app.analysis.version import ANALYSIS_VERSION
from app.core.constants import ResumeSection

# Below this, extraction is unreliable and the caller should be warned.
MIN_USEFUL_CHARS = 200


def analyze_resume_text(
    text: str, *, now: datetime | None = None
) -> ResumeProfileData:
    """Run the full deterministic analysis over resume text."""
    warnings: list[str] = []

    if len(text.strip()) < MIN_USEFUL_CHARS:
        warnings.append(
            "Resume text is very short; extraction may be incomplete."
        )

    sections = split_sections(text)
    section_kinds = [section.kind for section in sections]

    if ResumeSection.EXPERIENCE not in section_kinds:
        warnings.append(
            "No experience section detected; role detection relies on weaker "
            "signals."
        )
    if ResumeSection.SKILLS not in section_kinds:
        warnings.append("No skills section detected.")

    skills = extract_skills(sections)
    titles = extract_titles(sections)

    years, years_source, years_evidence = compute_years_experience(
        sections, text, now=now
    )
    seniority, seniority_confidence, seniority_evidence = detect_seniority(
        titles, sections, years
    )

    role = detect_target_role(titles, skills)
    domain = detect_domain(role, skills, sections, text)

    if role.key is None:
        warnings.append(
            "No target role could be determined from the resume. Set it "
            "manually before searching."
        )

    variations = (
        generate_role_variations(role.key, seniority) if role.key else []
    )

    return ResumeProfileData(
        analysis_version=ANALYSIS_VERSION,
        domain=domain,
        target_role=role,
        seniority=seniority,
        seniority_confidence=seniority_confidence,
        seniority_evidence=seniority_evidence,
        years_experience=years,
        years_experience_source=years_source,
        years_experience_evidence=years_evidence,
        skills=skills,
        titles=titles,
        responsibilities=extract_responsibilities(sections),
        education=extract_education(sections),
        certifications=extract_certifications(sections, text),
        role_variations=variations,
        sections_found=sorted(set(section_kinds), key=lambda kind: kind.value),
        warnings=warnings,
    )
