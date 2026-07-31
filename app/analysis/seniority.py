"""Seniority detection.

Title wording is authoritative when present - a resume saying "Senior
Financial Analyst" is a stronger claim than any inference from dates. Years of
experience is the fallback, and when neither is available the result is
``None`` rather than a guess.
"""

from __future__ import annotations

import re

from app.analysis.results import Evidence, ExtractedTitle, Section
from app.analysis.text import snippet
from app.core.constants import ResumeSection, Seniority

# Ordered most senior first so "senior vice president" is not read as "senior".
_TITLE_MARKERS: tuple[tuple[Seniority, str], ...] = (
    (Seniority.EXECUTIVE, r"\b(?:chief\s+\w+\s+officer|c[etofi]o|vice\s+president|vp)\b"),
    (Seniority.DIRECTOR, r"\b(?:director|head\s+of)\b"),
    (Seniority.PRINCIPAL, r"\bprincipal\b"),
    (Seniority.MANAGER, r"\b(?:manager|supervisor)\b"),
    (Seniority.LEAD, r"\b(?:lead|team\s+lead|tech\s+lead)\b"),
    (Seniority.SENIOR, r"\b(?:senior|sr\.?)\b"),
    (Seniority.JUNIOR, r"\b(?:junior|jr\.?)\b"),
    (Seniority.ENTRY, r"\b(?:entry[\s-]level|graduate|trainee|associate)\b"),
    (Seniority.INTERN, r"\b(?:intern|internship)\b"),
)

_COMPILED_MARKERS = tuple(
    (level, re.compile(pattern, re.IGNORECASE)) for level, pattern in _TITLE_MARKERS
)

# Fallback thresholds, in years. Deliberately conservative.
_YEARS_THRESHOLDS: tuple[tuple[float, Seniority], ...] = (
    (0.5, Seniority.ENTRY),
    (2.0, Seniority.JUNIOR),
    (5.0, Seniority.MID),
    (9.0, Seniority.SENIOR),
)


def detect_seniority(
    titles: list[ExtractedTitle],
    sections: list[Section],
    years_experience: float | None,
) -> tuple[Seniority | None, float, list[Evidence]]:
    """Return ``(seniority, confidence, evidence)``."""
    evidence: list[Evidence] = []

    # 1. The most recent title's wording.
    recent = next((title for title in titles if title.is_most_recent), None)
    if recent is not None:
        for level, pattern in _COMPILED_MARKERS:
            match = pattern.search(recent.raw)
            if match is None:
                continue
            evidence.append(
                Evidence(
                    kind="seniority_title_marker",
                    value=match.group(0),
                    section=recent.section,
                    line_number=recent.line_number,
                    snippet=recent.raw[:160],
                    weight=1.0,
                )
            )
            return level, 0.9, evidence

    # 2. A marker anywhere in the summary.
    summary = "\n".join(
        section.text for section in sections if section.kind is ResumeSection.SUMMARY
    )
    if summary:
        for level, pattern in _COMPILED_MARKERS:
            match = pattern.search(summary)
            if match is None:
                continue
            evidence.append(
                Evidence(
                    kind="seniority_summary_marker",
                    value=match.group(0),
                    section=ResumeSection.SUMMARY,
                    snippet=snippet(summary, match.start(), match.end()),
                    weight=0.6,
                )
            )
            return level, 0.55, evidence

    # 3. Years of experience.
    #
    # Deliberately ahead of older job titles: a candidate whose earliest role
    # was "Junior Software Engineer" but who now holds an unmarked title is
    # not junior. A stale title is the weakest signal available, so it is
    # consulted last (step 4) rather than allowed to override tenure.
    if years_experience is not None:
        level = Seniority.SENIOR
        for threshold, candidate in _YEARS_THRESHOLDS:
            if years_experience < threshold:
                level = candidate
                break
        evidence.append(
            Evidence(
                kind="seniority_from_years",
                value=f"{years_experience:g} years",
                section=ResumeSection.EXPERIENCE,
                snippet=f"Derived from {years_experience:g} years of experience.",
                weight=0.5,
            )
        )
        return level, 0.5, evidence

    # 4. Last resort: a marker on any older experience title.
    for title in titles:
        for level, pattern in _COMPILED_MARKERS:
            match = pattern.search(title.raw)
            if match is None:
                continue
            evidence.append(
                Evidence(
                    kind="seniority_older_title_marker",
                    value=match.group(0),
                    section=title.section,
                    line_number=title.line_number,
                    snippet=title.raw[:160],
                    weight=0.4,
                )
            )
            return level, 0.4, evidence

    # Nothing to go on.
    return None, 0.0, evidence
