"""Result types produced by the analysis pipeline.

Everything here is a plain dataclass with no database or HTTP coupling, so
extraction stays unit-testable in isolation.

Evidence is a first-class output, not a debugging afterthought: CLAUDE.md
requires that an incorrect role detection can be traced back to what the
extractor actually saw.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.constants import ResumeSection, Seniority, SkillKind


@dataclass(slots=True)
class Evidence:
    """One observation supporting a detection.

    ``snippet`` is a deliberately narrow excerpt - enough to explain the
    match, not enough to reconstruct the resume.
    """

    kind: str
    value: str
    section: ResumeSection = ResumeSection.OTHER
    line_number: int | None = None
    snippet: str = ""
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "section": self.section.value,
            "line_number": self.line_number,
            "snippet": self.snippet,
            "weight": round(self.weight, 4),
        }


@dataclass(slots=True)
class Section:
    """A contiguous block of the resume under one heading."""

    kind: ResumeSection
    heading: str
    start_line: int
    end_line: int
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(slots=True)
class ExtractedSkill:
    """A skill, technology or tool found in the resume."""

    key: str
    display: str
    kind: SkillKind
    occurrences: int = 0
    sections: set[ResumeSection] = field(default_factory=set)
    evidence: list[Evidence] = field(default_factory=list)
    confidence: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display": self.display,
            "kind": self.kind.value,
            "occurrences": self.occurrences,
            "sections": sorted(section.value for section in self.sections),
            "confidence": round(self.confidence, 4),
            "evidence": [item.as_dict() for item in self.evidence[:3]],
        }


@dataclass(slots=True)
class DateRange:
    """An employment period parsed from the experience section."""

    start_year: int
    start_month: int
    end_year: int | None
    end_month: int | None
    is_current: bool = False
    raw: str = ""

    @property
    def start_index(self) -> int:
        """Months since year 0, for interval arithmetic."""
        return self.start_year * 12 + (self.start_month - 1)

    def end_index(self, *, now_year: int, now_month: int) -> int:
        if self.is_current or self.end_year is None:
            return now_year * 12 + (now_month - 1)
        return self.end_year * 12 + ((self.end_month or 12) - 1)


@dataclass(slots=True)
class ExtractedTitle:
    """A job title found in the resume."""

    raw: str
    normalized: str
    role_key: str | None
    section: ResumeSection
    line_number: int
    is_most_recent: bool = False


@dataclass(slots=True)
class EducationItem:
    raw: str
    degree_level: str | None = None
    field_of_study: str | None = None
    institution: str | None = None
    year: int | None = None


@dataclass(slots=True)
class CertificationItem:
    raw: str
    name: str
    issuer: str | None = None
    year: int | None = None


@dataclass(slots=True)
class Detection:
    """A single winning classification with its runners-up.

    ``alternatives`` is retained for debugging only. It is never surfaced as a
    "suggested roles" workflow - CLAUDE.md forbids presenting alternative
    careers to the user.
    """

    key: str | None
    display: str | None
    confidence: float
    evidence: list[Evidence] = field(default_factory=list)
    alternatives: list[tuple[str, float]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display": self.display,
            "confidence": round(self.confidence, 4),
            "evidence": [item.as_dict() for item in self.evidence],
            "alternatives": [
                {"key": key, "score": round(score, 4)}
                for key, score in self.alternatives
            ],
        }


@dataclass(slots=True)
class ResumeProfileData:
    """Complete deterministic analysis of one resume."""

    analysis_version: str

    domain: Detection
    target_role: Detection

    seniority: Seniority | None = None
    seniority_confidence: float = 0.0
    seniority_evidence: list[Evidence] = field(default_factory=list)

    years_experience: float | None = None
    years_experience_source: str | None = None
    years_experience_evidence: list[Evidence] = field(default_factory=list)

    skills: list[ExtractedSkill] = field(default_factory=list)
    titles: list[ExtractedTitle] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    education: list[EducationItem] = field(default_factory=list)
    certifications: list[CertificationItem] = field(default_factory=list)

    role_variations: list[str] = field(default_factory=list)

    sections_found: list[ResumeSection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def skills_by_kind(self, kind: SkillKind) -> list[ExtractedSkill]:
        return [skill for skill in self.skills if skill.kind is kind]
