"""The job side of a match: what a posting actually asks for.

Extraction is deliberately narrow. **Not every word in a description is a
skill** - only terms registered in the taxonomy are recognised, so a posting
that happens to contain the word "excellent" does not acquire a skill, and a
description cannot inflate its own match by being long.

Requirements are split by the language around them, because a missing
must-have and a missing nice-to-have are not the same thing:

``required``   under a requirements heading, or stated with must/required
``preferred``  under a preferred heading, or hedged with preferred/plus/bonus
``mandatory``  a subset of required, stated in words that leave no doubt.
               Only these can cause a hard rejection.

Anything ambiguous lands in ``required`` at most, never ``mandatory``: an
uncertain requirement lowers the score, it does not reject the job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.analysis import taxonomy
from app.analysis.text import normalize_title, normalize_token
from app.core.constants import SENIORITY_RANK, Seniority
from app.processing.titles import extract_seniority

#: Below this a description is a teaser, not something a score can rest on.
#: CLAUDE.md section 7 rejects a job whose description cannot be scored
#: reliably rather than guessing at it.
MIN_SCOREABLE_DESCRIPTION = 200

# --- Section headings -------------------------------------------------------

_REQUIRED_HEADING = re.compile(
    r"""^\s*(?:
        (?:minimum|basic|essential|required|core)\s+(?:qualifications?|requirements?|skills?|experience)|
        (?:qualifications?|requirements?|what\s+you(?:'ll)?\s+need|
           who\s+you\s+are|must\s+haves?|you\s+(?:will\s+)?bring)
    )\s*[:\-]?\s*$""",
    flags=re.IGNORECASE | re.VERBOSE,
)

_PREFERRED_HEADING = re.compile(
    r"""^\s*(?:
        (?:preferred|desired|desirable|nice[\s-]to[\s-]have|bonus|additional|plus)\s*
        (?:qualifications?|requirements?|skills?|experience)?|
        (?:nice\s+to\s+haves?)|(?:what\s+will\s+set\s+you\s+apart)
    )\s*[:\-]?\s*$""",
    flags=re.IGNORECASE | re.VERBOSE,
)

# A heading that ends the requirements block: benefits, EEO boilerplate, etc.
_NEUTRAL_HEADING = re.compile(
    r"""^\s*(?:
        benefits?|perks?|compensation|salary|about\s+(?:us|the\s+(?:company|team|role))|
        responsibilities|what\s+you(?:'ll)?\s+do|the\s+role|equal\s+opportunity|
        our\s+(?:mission|values)|why\s+join
    ).*$""",
    flags=re.IGNORECASE | re.VERBOSE,
)

# --- Inline markers ---------------------------------------------------------

# Wording that leaves no doubt a requirement is mandatory. Only these can
# cause a rejection.
_MANDATORY_MARKER = re.compile(
    r"\b(?:must\s+(?:have|possess|hold|be)|is\s+required|are\s+required|"
    r"required\b(?!\s*(?:or|/)\s*preferred)|mandatory|essential\b)",
    flags=re.IGNORECASE,
)

# Wording that explicitly softens a requirement. Beats a mandatory marker and
# beats a required heading: "SQL required, Python preferred" is one of each.
_PREFERRED_MARKER = re.compile(
    r"\b(?:preferred|preferably|nice\s+to\s+have|a?\s*plus|bonus|desirable|"
    r"desired|ideally|advantageous|would\s+be\s+(?:great|nice)|not\s+required)\b",
    flags=re.IGNORECASE,
)

# --- Experience -------------------------------------------------------------

_YEARS_RANGE = re.compile(
    r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b",
    flags=re.IGNORECASE,
)
_YEARS_MINIMUM = re.compile(
    r"""(?:(?:at\s+least|minimum\s+(?:of\s+)?|min\.?\s*|over|more\s+than)\s*)?
        (\d{1,2})\s*\+?\s*(?:years?|yrs?)\b""",
    flags=re.IGNORECASE | re.VERBOSE,
)

# --- Education and certifications ------------------------------------------

_DEGREE_LEVELS: tuple[tuple[str, int], ...] = (
    ("doctorate", 4),
    ("ph.d", 4),
    ("phd", 4),
    ("master", 3),
    ("mba", 3),
    ("m.s", 3),
    ("msc", 3),
    ("bachelor", 2),
    ("b.s", 2),
    ("bsc", 2),
    ("b.a", 2),
    ("undergraduate degree", 2),
    ("associate degree", 1),
    ("high school", 0),
    ("diploma", 1),
)

# Certifications common enough to be named in a posting and verifiable in a
# resume. Deliberately a short list: a false "missing certification" rejection
# hides a job the candidate could have had.
_CERTIFICATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cpa", ("cpa", "certified public accountant")),
    ("cfa", ("cfa", "chartered financial analyst")),
    ("cma", ("cma", "certified management accountant")),
    ("frm", ("frm", "financial risk manager")),
    ("pmp", ("pmp", "project management professional")),
    ("cissp", ("cissp",)),
    ("cisa", ("cisa",)),
    ("ccna", ("ccna",)),
    ("aws certified", ("aws certified",)),
    ("azure certified", ("azure certified", "microsoft certified azure")),
    ("six sigma", ("six sigma",)),
    ("series 7", ("series 7",)),
    ("series 63", ("series 63",)),
)

# Which classification survives when a skill is named more than once.
_CONTEXT_STRENGTH: dict[str, int] = {
    "required": 2,
    "preferred": 1,
    "supporting": 0,
}

_CLEARANCE = re.compile(
    r"\b(?:security\s+clearance|ts/sci|top\s+secret|secret\s+clearance|"
    r"public\s+trust)\b",
    flags=re.IGNORECASE,
)


class SkillContext(StrEnum):
    """How firmly a posting asks for a skill."""

    #: Under a requirements heading, or stated with must/required wording.
    REQUIRED = "required"
    #: Hedged, or under a preferred heading.
    PREFERRED = "preferred"
    #: Mentioned in passing, with no requirement language around it. Not a
    #: requirement at all: a posting that says "you will own the monthly close"
    #: is describing the work, not listing a must-have.
    SUPPORTING = "supporting"


@dataclass(frozen=True, slots=True)
class SkillRequirement:
    """One taxonomy skill the posting names, and how firmly."""

    skill_key: str
    display: str
    context: SkillContext
    is_mandatory: bool
    evidence: str

    @property
    def is_required(self) -> bool:
        return self.context is SkillContext.REQUIRED

    @property
    def is_preferred(self) -> bool:
        return self.context is SkillContext.PREFERRED

    @property
    def weight_class(self) -> str:
        return "mandatory" if self.is_mandatory else self.context.value


@dataclass(frozen=True, slots=True)
class JobRequirements:
    """Everything scoring needs to know about one posting."""

    title: str
    role_phrase: str
    title_seniority: Seniority | None
    description_chars: int
    has_scoreable_description: bool

    skills: tuple[SkillRequirement, ...] = ()
    min_years: float | None = None
    max_years: float | None = None
    years_evidence: str | None = None

    required_degree_level: int | None = None
    degree_is_mandatory: bool = False
    required_certifications: tuple[str, ...] = ()
    mandatory_certifications: tuple[str, ...] = ()
    requires_clearance: bool = False

    #: Domain keywords the description uses, for the domain check.
    domain_keys: frozenset[str] = field(default_factory=frozenset)

    @property
    def required_skills(self) -> tuple[SkillRequirement, ...]:
        return tuple(skill for skill in self.skills if skill.is_required)

    @property
    def preferred_skills(self) -> tuple[SkillRequirement, ...]:
        return tuple(skill for skill in self.skills if skill.is_preferred)

    @property
    def supporting_skills(self) -> tuple[SkillRequirement, ...]:
        return tuple(
            skill for skill in self.skills if skill.context is SkillContext.SUPPORTING
        )

    @property
    def mandatory_skills(self) -> tuple[SkillRequirement, ...]:
        return tuple(skill for skill in self.skills if skill.is_mandatory)

    @property
    def title_seniority_rank(self) -> int | None:
        return SENIORITY_RANK[self.title_seniority] if self.title_seniority else None


def _classify_line(line: str, heading_context: str) -> tuple[SkillContext, bool]:
    """Return ``(context, is_mandatory)`` for one line of a description."""
    if _PREFERRED_MARKER.search(line):
        # An explicit softener always wins, even under a "Requirements"
        # heading: "SQL required, Python preferred" is one of each.
        return SkillContext.PREFERRED, False

    if _MANDATORY_MARKER.search(line):
        return SkillContext.REQUIRED, True

    if heading_context == "required":
        return SkillContext.REQUIRED, False
    if heading_context == "preferred":
        return SkillContext.PREFERRED, False

    return SkillContext.SUPPORTING, False


def extract_requirements(
    *, title: str, description: str | None, location: str | None = None
) -> JobRequirements:
    """Read one posting into the facts scoring is allowed to use.

    Pure: same inputs, same output, no clock and no network.
    """
    role_phrase = strip_seniority(title)
    title_seniority = extract_seniority(title)

    body = description or ""
    scoreable = len(body.strip()) >= MIN_SCOREABLE_DESCRIPTION

    skills: dict[str, SkillRequirement] = {}
    heading_context = "neutral"

    if scoreable:
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if _REQUIRED_HEADING.match(line):
                heading_context = "required"
                continue
            if _PREFERRED_HEADING.match(line):
                heading_context = "preferred"
                continue
            if _NEUTRAL_HEADING.match(line):
                heading_context = "neutral"
                continue

            context, is_mandatory = _classify_line(line, heading_context)
            for skill_key, display in _skills_in(line):
                existing = skills.get(skill_key)
                # A skill named twice keeps its strongest classification: a
                # must-have mentioned again in passing is still a must-have.
                if existing is not None and (
                    existing.is_mandatory
                    or _CONTEXT_STRENGTH[existing.context] >= _CONTEXT_STRENGTH[context]
                ):
                    continue
                skills[skill_key] = SkillRequirement(
                    skill_key=skill_key,
                    display=display,
                    context=context,
                    is_mandatory=is_mandatory,
                    evidence=line[:180],
                )

    min_years, max_years, years_evidence = _extract_years(body) if scoreable else (
        None,
        None,
        None,
    )
    degree_level, degree_mandatory = _extract_degree(body) if scoreable else (None, False)
    required_certs, mandatory_certs = (
        _extract_certifications(body) if scoreable else ((), ())
    )

    return JobRequirements(
        title=title,
        role_phrase=role_phrase,
        title_seniority=title_seniority,
        description_chars=len(body),
        has_scoreable_description=scoreable,
        skills=tuple(sorted(skills.values(), key=lambda s: s.skill_key)),
        min_years=min_years,
        max_years=max_years,
        years_evidence=years_evidence,
        required_degree_level=degree_level,
        degree_is_mandatory=degree_mandatory,
        required_certifications=required_certs,
        mandatory_certifications=mandatory_certs,
        requires_clearance=bool(_CLEARANCE.search(body)) if scoreable else False,
        domain_keys=_domains_in(f"{title}\n{body}\n{location or ''}"),
    )


def strip_seniority(title: str) -> str:
    """Normalise a title and remove seniority words, leaving the role itself.

    ``normalize_title`` already drops level markers and employment-type noise;
    this removes the seniority prefix and suffix on top, so "Sr. Financial
    Analyst" and "Financial Analyst" resolve to the same role.
    """
    normalized = normalize_title(title)
    tokens = [token for token in normalized.split() if token not in _SENIORITY_TOKENS]
    return " ".join(tokens).strip()


_SENIORITY_TOKENS = frozenset(
    {
        "senior",
        "sr",
        "sr.",
        "snr",
        "junior",
        "jr",
        "jr.",
        "jnr",
        "entry",
        "entry-level",
        "level",
        "mid",
        "intermediate",
        "experienced",
        "principal",
        "lead",
    }
)
# "Staff" is deliberately absent, matching the taxonomy's own note: "Staff
# Accountant" and "Staff Engineer" are established distinct titles, not a
# seniority prefix on a base title. Stripping it would turn "Staff Accountant"
# into "Accountant" and lose why the posting was rejected.


def _skills_in(line: str) -> list[tuple[str, str]]:
    """Taxonomy skills mentioned in one line, deduplicated."""
    found: dict[str, str] = {}

    for match in taxonomy.skill_pattern().finditer(line):
        key = taxonomy.skill_key_for_term(match.group(0))
        if key is None:
            continue
        spec = taxonomy.get_skill(key)
        found.setdefault(key, spec.display if spec else match.group(0))

    return list(found.items())


def _domains_in(text: str) -> frozenset[str]:
    keys: set[str] = set()
    for match in taxonomy.domain_keyword_pattern().finditer(text):
        key = taxonomy.domain_key_for_keyword(match.group(0))
        if key:
            keys.add(key)
    return frozenset(keys)


def _extract_years(body: str) -> tuple[float | None, float | None, str | None]:
    """Find a stated experience requirement, or return nothing.

    A range wins over a bare minimum, and the *lowest* stated minimum wins over
    a higher one: a posting saying "3+ years finance, 5+ years leadership"
    is asking for three years of the job's core work.
    """
    best_min: float | None = None
    best_max: float | None = None
    evidence: str | None = None

    for line in body.splitlines():
        range_match = _YEARS_RANGE.search(line)
        if range_match is not None:
            low, high = float(range_match.group(1)), float(range_match.group(2))
            if low <= high and (best_min is None or low < best_min):
                best_min, best_max, evidence = low, high, line.strip()[:180]
            continue

        minimum_match = _YEARS_MINIMUM.search(line)
        if minimum_match is not None:
            value = float(minimum_match.group(1))
            # Two-digit years are almost always a date or a salary fragment.
            if 0 < value <= 20 and (best_min is None or value < best_min):
                best_min, evidence = value, line.strip()[:180]

    return best_min, best_max, evidence


def _extract_degree(body: str) -> tuple[int | None, bool]:
    """Highest degree level the posting asks for, and whether it is a must."""
    level: int | None = None
    mandatory = False

    for line in body.splitlines():
        lowered = line.lower()
        for term, term_level in _DEGREE_LEVELS:
            if term not in lowered:
                continue
            hedged = bool(_PREFERRED_MARKER.search(line))
            if level is None or term_level > level:
                level = term_level
                mandatory = not hedged and bool(_MANDATORY_MARKER.search(line))
            elif term_level == level and not hedged and _MANDATORY_MARKER.search(line):
                mandatory = True
            break

    return level, mandatory


def _extract_certifications(body: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    required: set[str] = set()
    mandatory: set[str] = set()

    for line in body.splitlines():
        lowered = normalize_token(line)
        if not lowered:
            continue

        hedged = bool(_PREFERRED_MARKER.search(line))
        is_mandatory = not hedged and bool(_MANDATORY_MARKER.search(line))

        for name, aliases in _CERTIFICATIONS:
            if not any(re.search(rf"\b{re.escape(a)}\b", lowered) for a in aliases):
                continue
            required.add(name)
            if is_mandatory:
                mandatory.add(name)

    return tuple(sorted(required)), tuple(sorted(mandatory))


def resume_has_certification(certifications: tuple[str, ...], name: str) -> bool:
    """Does the resume evidence this certification?"""
    aliases = next((a for n, a in _CERTIFICATIONS if n == name), (name,))
    haystack = " ".join(certifications)

    return any(re.search(rf"\b{re.escape(alias)}\b", haystack) for alias in aliases)


def resume_degree_level(education: tuple[str, ...]) -> int | None:
    """Highest degree level the resume evidences, or None."""
    level: int | None = None
    haystack = " ".join(education)

    for term, term_level in _DEGREE_LEVELS:
        if term in haystack and (level is None or term_level > level):
            level = term_level

    return level
