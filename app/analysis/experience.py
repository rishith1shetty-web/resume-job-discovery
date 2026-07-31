"""Years-of-experience computation.

Two independent sources, in priority order:

1. Employment date ranges in the experience section, merged so overlapping or
   concurrent roles are not double-counted.
2. An explicit statement such as "5+ years of experience".

Date ranges win when present because they are verifiable. If neither exists
the result is ``None`` - experience is never guessed (CLAUDE.md: do not infer
unsupported experience).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.analysis.results import DateRange, Evidence, Section
from app.analysis.text import snippet
from app.core.constants import ResumeSection

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_CURRENT = r"present|current|now|to\s*date|ongoing"
_SEP = r"\s*(?:-|–|—|to|until|through)\s*"

# "Jan 2019 - Mar 2022" / "January 2019 to Present"
_MONTH_YEAR_RANGE = re.compile(
    rf"\b(?P<sm>{_MONTH_ALT})\.?,?\s*(?P<sy>(?:19|20)\d{{2}})"
    rf"{_SEP}"
    rf"(?:(?P<cur>{_CURRENT})|(?P<em>{_MONTH_ALT})\.?,?\s*(?P<ey>(?:19|20)\d{{2}}))",
    re.IGNORECASE,
)

# "03/2019 - 05/2021" / "3-2019 to present"
_NUMERIC_RANGE = re.compile(
    rf"\b(?P<sm>0?[1-9]|1[0-2])[/\-.](?P<sy>(?:19|20)\d{{2}})"
    rf"{_SEP}"
    rf"(?:(?P<cur>{_CURRENT})|(?P<em>0?[1-9]|1[0-2])[/\-.](?P<ey>(?:19|20)\d{{2}}))",
    re.IGNORECASE,
)

# "2019 - 2022" / "2019 to present". Checked last: the widest and least precise.
_YEAR_RANGE = re.compile(
    rf"\b(?P<sy>(?:19|20)\d{{2}})"
    rf"{_SEP}"
    rf"(?:(?P<cur>{_CURRENT})|(?P<ey>(?:19|20)\d{{2}}))\b",
    re.IGNORECASE,
)

# "5+ years of experience", "over 7 years' experience"
_EXPLICIT_YEARS = re.compile(
    r"\b(?:over|more\s+than|nearly|about|approx(?:\.|imately)?\s*)?"
    r"(?P<years>\d{1,2})\s*\+?\s*"
    r"(?:years?|yrs?)[\s'’]*(?:of\s+)?(?:professional\s+|relevant\s+|work\s+)?"
    r"experience",
    re.IGNORECASE,
)


def _month(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip().lower().rstrip(".")
    if value.isdigit():
        number = int(value)
        return number if 1 <= number <= 12 else None
    return _MONTHS.get(value)


def parse_date_ranges(text: str) -> list[DateRange]:
    """Find employment periods, most precise pattern first.

    Spans already consumed by a more precise pattern are not reconsidered, so
    "Jan 2019 - Mar 2022" is never also read as the bare year range "2019 -
    2022".
    """
    ranges: list[DateRange] = []
    consumed: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < c_end and c_start < end for c_start, c_end in consumed)

    for pattern, numeric in (
        (_MONTH_YEAR_RANGE, False),
        (_NUMERIC_RANGE, True),
        (_YEAR_RANGE, False),
    ):
        for match in pattern.finditer(text):
            if overlaps(match.start(), match.end()):
                continue

            groups = match.groupdict()
            start_year = int(groups["sy"])
            start_month = _month(groups.get("sm")) or 1
            is_current = bool(groups.get("cur"))

            end_year = int(groups["ey"]) if groups.get("ey") else None
            end_month = _month(groups.get("em")) or (12 if end_year else None)

            if end_year is not None and end_year < start_year:
                continue  # nonsensical range; ignore rather than guess

            ranges.append(
                DateRange(
                    start_year=start_year,
                    start_month=start_month,
                    end_year=end_year,
                    end_month=end_month,
                    is_current=is_current,
                    raw=match.group(0).strip(),
                )
            )
            consumed.append((match.start(), match.end()))
            _ = numeric

    return ranges


def total_months(ranges: list[DateRange], *, now: datetime | None = None) -> int:
    """Merge overlapping periods and total the months covered.

    Merging matters: concurrent roles, or a promotion listed as two entries at
    the same employer, must not inflate the total.
    """
    if not ranges:
        return 0

    now = now or datetime.now(UTC)
    intervals: list[tuple[int, int]] = []

    for date_range in ranges:
        start = date_range.start_index
        end = date_range.end_index(now_year=now.year, now_month=now.month)
        if end < start:
            continue
        # +1 so a single month counts as one month, not zero.
        intervals.append((start, end + 1))

    if not intervals:
        return 0

    intervals.sort()
    merged: list[list[int]] = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return sum(end - start for start, end in merged)


def find_explicit_years(text: str) -> tuple[float, str] | None:
    """Largest explicitly stated years-of-experience claim, with its snippet."""
    best: tuple[float, str] | None = None

    for match in _EXPLICIT_YEARS.finditer(text):
        years = float(match.group("years"))
        if years > 60:  # implausible; treat as a false positive
            continue
        if best is None or years > best[0]:
            best = (years, snippet(text, match.start(), match.end()))

    return best


def compute_years_experience(
    sections: list[Section],
    full_text: str,
    *,
    now: datetime | None = None,
) -> tuple[float | None, str | None, list[Evidence]]:
    """Return ``(years, source, evidence)``.

    ``source`` is ``"date_ranges"``, ``"explicit_statement"`` or ``None``.
    """
    evidence: list[Evidence] = []

    experience_text = "\n".join(
        section.text
        for section in sections
        if section.kind in (ResumeSection.EXPERIENCE, ResumeSection.PROJECTS)
    )
    # Fall back to the whole document when no experience section was detected.
    search_text = experience_text or full_text

    ranges = parse_date_ranges(search_text)
    if ranges:
        months = total_months(ranges, now=now)
        if months > 0:
            for date_range in ranges[:4]:
                evidence.append(
                    Evidence(
                        kind="date_range",
                        value=date_range.raw,
                        section=ResumeSection.EXPERIENCE,
                        snippet=date_range.raw,
                        weight=1.0,
                    )
                )
            return round(months / 12, 1), "date_ranges", evidence

    explicit = find_explicit_years(full_text)
    if explicit is not None:
        years, text_snippet = explicit
        evidence.append(
            Evidence(
                kind="explicit_years",
                value=f"{years:g} years",
                section=ResumeSection.SUMMARY,
                snippet=text_snippet,
                weight=0.8,
            )
        )
        return years, "explicit_statement", evidence

    # Nothing verifiable. Never guess.
    return None, None, evidence
