"""Education, certification and responsibility extraction.

All three are conservative: only what the resume states is recorded. Nothing
is normalised into a qualification the candidate did not claim (CLAUDE.md: do
not infer unsupported education).
"""

from __future__ import annotations

import re

from app.analysis.results import CertificationItem, EducationItem, Section
from app.analysis.text import strip_bullet
from app.core.constants import ResumeSection

# Degree levels, most specific first so "master of science" beats "master".
_DEGREE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("doctorate", r"\b(?:ph\.?d|doctor of philosophy|doctorate|d\.?phil)\b"),
    ("doctorate", r"\b(?:m\.?d|j\.?d|ed\.?d)\b"),
    ("master", r"\b(?:m\.?b\.?a|master'?s?|m\.?sc|m\.?s\b|m\.?a\b|m\.?eng|m\.?tech)\b"),
    ("bachelor", r"\b(?:bachelor'?s?|b\.?sc|b\.?s\b|b\.?a\b|b\.?eng|b\.?tech|b\.?com)\b"),
    ("associate", r"\b(?:associate'?s?\s+degree|a\.?a\b|a\.?s\b)\b"),
    ("diploma", r"\b(?:diploma|higher national diploma|hnd)\b"),
    ("certificate", r"\b(?:certificate program|postgraduate certificate)\b"),
)

_FIELD_RE = re.compile(
    r"\b(?:in|of)\s+([A-Za-z][A-Za-z&\s/,-]{2,60}?)"
    r"(?=\s*(?:[,|(]|\bfrom\b|\bat\b|\d{4}|$))",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_INSTITUTION_RE = re.compile(
    r"\b((?:[A-Z][\w.&'-]*\s+)*"
    r"(?:University|College|Institute|Academy|School|Polytechnic)"
    r"(?:\s+of\s+[A-Z][\w.&'-]*(?:\s+[A-Z][\w.&'-]*)*)?)"
)

# Well-known certifications. Matching a registry avoids treating arbitrary
# capitalised phrases as credentials.
_KNOWN_CERTIFICATIONS: tuple[tuple[str, str], ...] = (
    ("CPA", r"\bC\.?P\.?A\.?\b|\bcertified public accountant\b"),
    ("CFA", r"\bC\.?F\.?A\.?\b|\bchartered financial analyst\b"),
    ("CMA", r"\bC\.?M\.?A\.?\b|\bcertified management accountant\b"),
    ("ACCA", r"\bACCA\b"),
    ("PMP", r"\bP\.?M\.?P\.?\b|\bproject management professional\b"),
    ("CSM", r"\bcertified scrum master\b|\bCSM\b"),
    ("CISSP", r"\bCISSP\b"),
    ("CISM", r"\bCISM\b"),
    ("CEH", r"\bcertified ethical hacker\b|\bCEH\b"),
    ("CompTIA Security+", r"\bsecurity\+\b|\bcomptia security\b"),
    ("AWS Certified Solutions Architect", r"\baws certified solutions architect\b"),
    ("AWS Certified Developer", r"\baws certified developer\b"),
    ("Azure Administrator", r"\bazure administrator\b|\baz-104\b"),
    ("Google Cloud Professional", r"\bgoogle cloud (?:certified )?professional\b"),
    ("CCNA", r"\bCCNA\b"),
    ("Six Sigma Green Belt", r"\bsix sigma green belt\b"),
    ("Six Sigma Black Belt", r"\bsix sigma black belt\b"),
    ("SHRM-CP", r"\bSHRM-?CP\b"),
    ("PHR", r"\bPHR\b"),
    ("RN License", r"\bregistered nurse license\b|\bRN license\b"),
    ("BLS", r"\bbasic life support\b|\bBLS\b"),
    ("ACLS", r"\badvanced cardiac life support\b|\bACLS\b"),
    ("Google Analytics Certification", r"\bgoogle analytics (?:individual )?certifi"),
    ("HubSpot Certification", r"\bhubspot (?:inbound )?certifi"),
    ("Tableau Desktop Specialist", r"\btableau desktop specialist\b"),
)

_COMPILED_DEGREES = tuple(
    (level, re.compile(pattern, re.IGNORECASE)) for level, pattern in _DEGREE_PATTERNS
)
_COMPILED_CERTS = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _KNOWN_CERTIFICATIONS
)


def extract_education(sections: list[Section]) -> list[EducationItem]:
    """Parse degree entries from the education section."""
    items: list[EducationItem] = []
    seen: set[str] = set()

    for section in sections:
        if section.kind is not ResumeSection.EDUCATION:
            continue

        for line in section.lines:
            cleaned = strip_bullet(line)
            if not cleaned or len(cleaned) < 4:
                continue

            level = next(
                (lvl for lvl, pattern in _COMPILED_DEGREES if pattern.search(cleaned)),
                None,
            )
            if level is None:
                continue

            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)

            field_match = _FIELD_RE.search(cleaned)
            institution_match = _INSTITUTION_RE.search(cleaned)
            year_match = _YEAR_RE.search(cleaned)

            items.append(
                EducationItem(
                    raw=cleaned,
                    degree_level=level,
                    field_of_study=(
                        field_match.group(1).strip(" ,-") if field_match else None
                    ),
                    institution=(
                        institution_match.group(1).strip() if institution_match else None
                    ),
                    year=int(year_match.group(0)) if year_match else None,
                )
            )

    return items


def extract_certifications(sections: list[Section], full_text: str) -> list[CertificationItem]:
    """Find recognised certifications.

    Searches the certifications section first, then the whole document, since
    credentials are often listed beside the candidate's name.
    """
    certification_text = "\n".join(
        section.text
        for section in sections
        if section.kind is ResumeSection.CERTIFICATIONS
    )
    haystack = f"{certification_text}\n{full_text}"

    items: list[CertificationItem] = []
    seen: set[str] = set()

    for name, pattern in _COMPILED_CERTS:
        match = pattern.search(haystack)
        if match is None or name in seen:
            continue
        seen.add(name)

        line_start = haystack.rfind("\n", 0, match.start()) + 1
        line_end = haystack.find("\n", match.end())
        raw = haystack[line_start : line_end if line_end != -1 else len(haystack)].strip()

        year_match = _YEAR_RE.search(raw)
        items.append(
            CertificationItem(
                raw=strip_bullet(raw)[:200],
                name=name,
                year=int(year_match.group(0)) if year_match else None,
            )
        )

    return items


# A responsibility line starts with an action verb; this is what separates
# "Built financial models" from a heading or a company name.
_ACTION_VERBS = (
    "achieved", "administered", "analyzed", "analysed", "audited", "automated",
    "built", "collaborated", "conducted", "configured", "consolidated",
    "coordinated", "created", "decreased", "defined", "delivered", "deployed",
    "designed", "developed", "diagnosed", "directed", "documented", "drove",
    "engineered", "enhanced", "ensured", "established", "evaluated", "executed",
    "expanded", "facilitated", "forecasted", "generated", "handled",
    "implemented", "improved", "increased", "influenced", "initiated",
    "integrated", "launched", "led", "maintained", "managed", "mentored",
    "migrated", "monitored", "negotiated", "operated", "optimized", "optimised",
    "orchestrated", "organized", "organised", "oversaw", "partnered",
    "performed", "planned", "prepared", "presented", "processed", "produced",
    "provided", "reconciled", "reduced", "refactored", "reported", "researched",
    "resolved", "reviewed", "scaled", "spearheaded", "streamlined",
    "supervised", "supported", "tested", "tracked", "trained", "transformed",
    "troubleshot", "validated", "wrote",
)
_ACTION_VERB_RE = re.compile(
    rf"^(?:{'|'.join(_ACTION_VERBS)})\b", re.IGNORECASE
)

_MAX_RESPONSIBILITIES = 40


def extract_responsibilities(sections: list[Section]) -> list[str]:
    """Collect responsibility and achievement bullets."""
    results: list[str] = []
    seen: set[str] = set()

    for section in sections:
        if section.kind not in (
            ResumeSection.EXPERIENCE,
            ResumeSection.PROJECTS,
            ResumeSection.ACHIEVEMENTS,
        ):
            continue

        for line in section.lines:
            cleaned = strip_bullet(line)
            if len(cleaned) < 20 or len(cleaned) > 400:
                continue
            if not _ACTION_VERB_RE.match(cleaned):
                continue

            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            results.append(cleaned)

            if len(results) >= _MAX_RESPONSIBILITIES:
                return results

    return results
