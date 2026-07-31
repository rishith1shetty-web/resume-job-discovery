"""Split a resume into labelled sections.

Section context is what makes the rest of extraction reliable: a skill listed
under EXPERIENCE is stronger evidence than one in a keyword dump, and a title
found under EXPERIENCE is a job the candidate held rather than a course name.
"""

from __future__ import annotations

import re

from app.analysis.results import Section
from app.analysis.text import normalize_token
from app.core.constants import ResumeSection

# Heading phrases mapped to their section kind. Matched against a normalised,
# punctuation-stripped form of the line.
_HEADING_MAP: dict[str, ResumeSection] = {
    # Summary
    "summary": ResumeSection.SUMMARY,
    "professional summary": ResumeSection.SUMMARY,
    "career summary": ResumeSection.SUMMARY,
    "profile": ResumeSection.SUMMARY,
    "professional profile": ResumeSection.SUMMARY,
    "objective": ResumeSection.SUMMARY,
    "career objective": ResumeSection.SUMMARY,
    "about me": ResumeSection.SUMMARY,
    "overview": ResumeSection.SUMMARY,
    # Experience
    "experience": ResumeSection.EXPERIENCE,
    "work experience": ResumeSection.EXPERIENCE,
    "professional experience": ResumeSection.EXPERIENCE,
    "employment history": ResumeSection.EXPERIENCE,
    "employment": ResumeSection.EXPERIENCE,
    "work history": ResumeSection.EXPERIENCE,
    "career history": ResumeSection.EXPERIENCE,
    "relevant experience": ResumeSection.EXPERIENCE,
    "professional background": ResumeSection.EXPERIENCE,
    # Education
    "education": ResumeSection.EDUCATION,
    "academic background": ResumeSection.EDUCATION,
    "academic qualifications": ResumeSection.EDUCATION,
    "educational qualifications": ResumeSection.EDUCATION,
    "qualifications": ResumeSection.EDUCATION,
    # Skills
    "skills": ResumeSection.SKILLS,
    "technical skills": ResumeSection.SKILLS,
    "core skills": ResumeSection.SKILLS,
    "key skills": ResumeSection.SKILLS,
    "core competencies": ResumeSection.SKILLS,
    "competencies": ResumeSection.SKILLS,
    "areas of expertise": ResumeSection.SKILLS,
    "technical proficiencies": ResumeSection.SKILLS,
    "technologies": ResumeSection.SKILLS,
    "tools": ResumeSection.SKILLS,
    "tools and technologies": ResumeSection.SKILLS,
    "skills and tools": ResumeSection.SKILLS,
    # Certifications
    "certifications": ResumeSection.CERTIFICATIONS,
    "certification": ResumeSection.CERTIFICATIONS,
    "licenses": ResumeSection.CERTIFICATIONS,
    "licenses and certifications": ResumeSection.CERTIFICATIONS,
    "certifications and licenses": ResumeSection.CERTIFICATIONS,
    "professional certifications": ResumeSection.CERTIFICATIONS,
    "credentials": ResumeSection.CERTIFICATIONS,
    # Projects
    "projects": ResumeSection.PROJECTS,
    "key projects": ResumeSection.PROJECTS,
    "personal projects": ResumeSection.PROJECTS,
    "selected projects": ResumeSection.PROJECTS,
    # Achievements
    "achievements": ResumeSection.ACHIEVEMENTS,
    "key achievements": ResumeSection.ACHIEVEMENTS,
    "accomplishments": ResumeSection.ACHIEVEMENTS,
    "awards": ResumeSection.ACHIEVEMENTS,
    "awards and honors": ResumeSection.ACHIEVEMENTS,
    "honors": ResumeSection.ACHIEVEMENTS,
}

# A heading is short. Anything longer is prose that merely starts with a
# heading-like word.
_MAX_HEADING_WORDS = 5
_MAX_HEADING_CHARS = 60

_TRAILING_PUNCT_RE = re.compile(r"[:\-–—_=.\s]+$")
_LEADING_PUNCT_RE = re.compile(r"^[\s\-–—_=*•#]+")


def _heading_kind(line: str) -> ResumeSection | None:
    """Return the section kind if this line looks like a heading."""
    stripped = _TRAILING_PUNCT_RE.sub("", _LEADING_PUNCT_RE.sub("", line)).strip()

    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return None
    if len(stripped.split()) > _MAX_HEADING_WORDS:
        return None
    # A line ending in a sentence period is prose, not a heading.
    if line.rstrip().endswith(".") and not line.rstrip().endswith(".."):
        return None

    return _HEADING_MAP.get(normalize_token(stripped))


def split_sections(text: str) -> list[Section]:
    """Split resume text into sections.

    Content appearing before the first recognised heading becomes an
    ``OTHER`` section - typically the name/contact block, and occasionally an
    unlabelled summary.
    """
    lines = text.split("\n")
    sections: list[Section] = []

    current_kind = ResumeSection.OTHER
    current_heading = ""
    current_start = 0
    buffer: list[str] = []

    def flush(end_line: int) -> None:
        if buffer or current_heading:
            sections.append(
                Section(
                    kind=current_kind,
                    heading=current_heading,
                    start_line=current_start,
                    end_line=end_line,
                    lines=list(buffer),
                )
            )

    for index, line in enumerate(lines):
        kind = _heading_kind(line)
        if kind is not None:
            flush(index - 1)
            current_kind = kind
            current_heading = line.strip()
            current_start = index
            buffer = []
        else:
            buffer.append(line)

    flush(len(lines) - 1)

    return [section for section in sections if section.lines or section.heading]


def section_for_line(sections: list[Section], line_number: int) -> ResumeSection:
    """Which section a given line falls in."""
    for section in sections:
        if section.start_line <= line_number <= section.end_line:
            return section.kind
    return ResumeSection.OTHER


def get_section(sections: list[Section], kind: ResumeSection) -> Section | None:
    """First section of the requested kind, if present."""
    for section in sections:
        if section.kind is kind:
            return section
    return None


def section_text(sections: list[Section], kind: ResumeSection) -> str:
    """Combined text of every section of one kind."""
    return "\n".join(
        section.text for section in sections if section.kind is kind
    ).strip()
