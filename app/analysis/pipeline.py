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
from app.analysis.results import Detection, ExtractedTitle, ResumeProfileData
from app.analysis.sections import split_sections
from app.analysis.seniority import detect_seniority
from app.analysis.skills import extract_skills
from app.analysis.titles import extract_titles
from app.analysis.variations import generate_role_variations
from app.analysis.version import ANALYSIS_VERSION
from app.core.constants import ResumeSection

# Below this, extraction is unreliable and the caller should be warned.
MIN_USEFUL_CHARS = 200

#: Role confidence below which the detection should be reviewed before it is
#: searched on. Confidence blends evidence strength with the margin over the
#: runner-up, so a value this low means either almost nothing supported the
#: winner or nothing separated it from the next role. Measured over the stored
#: resumes, a detection backed by an actual job title scores 0.88-0.94, while
#: one resting on skill overlap alone scores 0.2 - the gap is wide enough that
#: a single threshold separates them cleanly.
LOW_ROLE_CONFIDENCE = 0.4

#: Domain detection with no role supplied. The role is normally the dominant
#: input to the domain, so a domain read back from the role cannot corroborate
#: it; this one is built from the resume's keywords and skills alone and is
#: used only to break an exact tie between roles.
_NO_ROLE = Detection(key=None, display=None, confidence=0.0)


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

    evidence_domain = detect_domain(_NO_ROLE, skills, sections, text)
    role = detect_target_role(titles, skills, domain_hint=evidence_domain.key)
    domain = detect_domain(role, skills, sections, text)

    if role.key is None:
        warnings.append(
            "No target role could be determined from the resume. Set it "
            "manually before searching."
        )
    else:
        warnings.extend(_role_review_warnings(role, titles))

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


def _role_review_warnings(
    role: Detection, titles: list[ExtractedTitle]
) -> list[str]:
    """Warn when the detected role deserves a look before it is searched on.

    The user may always continue - CLAUDE.md section 3 gives them the
    correction, not a gate. What this prevents is a coin-flip being presented
    with the same quiet certainty as a detection backed by four job titles.
    """
    warnings: list[str] = []

    if role.is_tied:
        others = ", ".join(role.tied_with)
        resolved = (
            "resolved in favour of the resume's own domain"
            if role.tie_break == "domain_agreement"
            else "resolved only by deterministic ordering"
        )
        warnings.append(
            f"Target role {role.display!r} scored exactly the same as "
            f"{others} and was {resolved}. Review the target role before "
            "searching."
        )

    if role.confidence < LOW_ROLE_CONFIDENCE and not role.is_tied:
        warnings.append(
            f"Target role {role.display!r} was detected with low confidence "
            f"({role.confidence:.2f}). Review the target role before "
            "searching."
        )

    if not any(title.role_key is not None for title in titles):
        warnings.append(
            "No recognised job title was found in the resume, so the target "
            "role rests on skill overlap alone. Review the target role before "
            "searching."
        )

    return warnings
