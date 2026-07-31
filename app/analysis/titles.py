"""Job-title extraction.

Titles carry the most weight in role detection, so they are located precisely
and ordered by recency. The most recent title is the strongest single signal
of what the candidate is currently doing.
"""

from __future__ import annotations

import re

from app.analysis import taxonomy
from app.analysis.experience import parse_date_ranges
from app.analysis.results import ExtractedTitle, Section
from app.analysis.text import normalize_title, strip_bullet
from app.core.constants import ResumeSection

# Sections where a matched title means "a job the candidate held".
_JOB_TITLE_SECTIONS = (ResumeSection.EXPERIENCE, ResumeSection.SUMMARY)

# Lines longer than this are prose that merely mentions a title.
_MAX_TITLE_LINE_CHARS = 120

_SEPARATORS_RE = re.compile(r"\s+(?:at|@|\||[-–—]|,)\s+", re.IGNORECASE)


def _candidate_title_fragments(line: str) -> list[str]:
    """Split "Senior Financial Analyst | Acme Corp | 2019-2022" into parts.

    The title is usually the first fragment, but splitting lets a title be
    found wherever it sits on the line.
    """
    cleaned = strip_bullet(line)
    if not cleaned or len(cleaned) > _MAX_TITLE_LINE_CHARS:
        return []
    return [part.strip() for part in _SEPARATORS_RE.split(cleaned) if part.strip()]


def extract_titles(sections: list[Section]) -> list[ExtractedTitle]:
    """Find job titles, ordered as they appear in the document.

    ``is_most_recent`` marks the first title in the experience section, which
    is the conventional resume ordering (newest first). When date ranges make
    the ordering explicit, that evidence is preferred.
    """
    pattern = taxonomy.role_pattern()
    titles: list[ExtractedTitle] = []

    for section in sections:
        if section.kind not in _JOB_TITLE_SECTIONS:
            continue

        for offset, line in enumerate(section.lines):
            for fragment in _candidate_title_fragments(line):
                match = pattern.search(fragment)
                if match is None:
                    continue

                role_key = taxonomy.role_key_for_term(match.group(0))
                titles.append(
                    ExtractedTitle(
                        raw=fragment,
                        normalized=normalize_title(match.group(0)),
                        role_key=role_key,
                        section=section.kind,
                        line_number=section.start_line + offset + 1,
                    )
                )
                break  # one title per line is enough

    _mark_most_recent(titles, sections)
    return titles


def _mark_most_recent(titles: list[ExtractedTitle], sections: list[Section]) -> None:
    """Flag the current/most recent job title."""
    experience_titles = [
        title for title in titles if title.section is ResumeSection.EXPERIENCE
    ]
    if not experience_titles:
        # Fall back to a summary title, which usually states the target role.
        if titles:
            titles[0].is_most_recent = True
        return

    # A title on a line that also mentions an ongoing period is definitively
    # current, and beats positional ordering.
    for title in experience_titles:
        line = _line_at(sections, title.line_number)
        if line and any(
            date_range.is_current for date_range in parse_date_ranges(line)
        ):
            title.is_most_recent = True
            return

    # Otherwise assume conventional newest-first ordering.
    experience_titles[0].is_most_recent = True


def _line_at(sections: list[Section], line_number: int) -> str | None:
    for section in sections:
        if section.start_line <= line_number - 1 <= section.end_line:
            index = line_number - 1 - section.start_line
            if 0 <= index < len(section.lines):
                return section.lines[index]
    return None


def most_recent_title(titles: list[ExtractedTitle]) -> ExtractedTitle | None:
    for title in titles:
        if title.is_most_recent:
            return title
    return titles[0] if titles else None
