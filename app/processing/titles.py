"""Job-title normalisation and seniority extraction.

Titles are the noisiest field a portal returns. The same vacancy appears as
"Senior Financial Analyst", "Sr. Financial Analyst (Remote)" and
"Financial Analyst, Senior - Req #48210" depending on who republished it.

Two things come out of here:

* a **display** title, cleaned of formatting noise but still the portal's words
* a **key** for comparison, with qualifiers and requisition numbers removed

and two things that must *never* be collapsed away, because they distinguish
genuinely different vacancies: seniority ("Senior" vs "Junior") and level
("Analyst II" vs "Analyst III").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.constants import SENIORITY_RANK, Seniority, WorkMode
from app.processing.text import clean_display_text, comparison_key, similarity

# --- Noise ------------------------------------------------------------------

# Requisition numbers in every shape portals publish them.
_REQUISITION = re.compile(
    r"""
    [\(\[\-–—,]?\s*
    (?:job\s*(?:id|code|ref)|req(?:uisition)?\.?\s*(?:id|no\.?|number|\#)?|
       posting\s*id|ref(?:erence)?\.?\s*(?:no\.?|\#)?|id)
    \s*[:\#]?\s*[A-Za-z]{0,4}[-_]?\d{3,}
    \s*[\)\]]?
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)
_BARE_HASH_NUMBER = re.compile(r"[\(\[]?\s*#\s*\d{3,}\s*[\)\]]?")

# Recruiter decoration that carries no information about the role.
_MARKETING = re.compile(
    r"""
    \b(?:
        urgent(?:ly)?\s*(?:hiring|required)? | now\s+hiring | hiring\s+now |
        immediate\s+(?:start|joining|opening) | apply\s+now | new\s*!? |
        featured | hot\s+job | walk[\s-]?in
    )\b
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

# Equal-opportunity and gender markers common on European postings.
_GENDER_MARKER = re.compile(
    r"[\(\[]\s*[mwfdhx](?:\s*[/|]\s*[mwfdhx])+\s*[\)\]]", flags=re.IGNORECASE
)

# Work arrangement, employment type and similar qualifiers. Removed from the
# comparison key only: they are real information, just not part of the role's
# identity, and they are captured separately as work mode and employment type.
_QUALIFIER_WORDS = frozenset(
    {
        "remote",
        "hybrid",
        "onsite",
        "on",
        "site",
        "wfh",
        "telecommute",
        "virtual",
        "fulltime",
        "full",
        "part",
        "time",
        "contract",
        "contractor",
        "permanent",
        "temporary",
        "temp",
        "freelance",
        "w2",
        "c2c",
        "1099",
        "usa",
        "us",
        "uk",
        "hiring",
        "opening",
        "openings",
        "opportunity",
        "position",
        "role",
        "vacancy",
        "job",
        "jobs",
        "m",
        "f",
        "d",
        "w",
        "x",
    }
)

_EMPTY_BRACKETS = re.compile(r"[\(\[]\s*[\)\]]")

# Abbreviations expanded in the comparison key only. "Sr. Financial Analyst"
# and "Senior Financial Analyst" are one vacancy on two portals; without this
# they share only two of four words and never match.
_ABBREVIATIONS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "jnr": "junior",
    "mgr": "manager",
    "dir": "director",
    "vp": "vice president",
    "asst": "assistant",
    "assoc": "associate",
    "eng": "engineer",
    "engr": "engineer",
    "dev": "developer",
    "admin": "administrator",
    "acct": "accountant",
    "acctg": "accounting",
    "ops": "operations",
    "mktg": "marketing",
    "qa": "quality assurance",
}
# Deliberately absent: "pm" (project or product manager?), "ba" (business
# analyst or bachelor?), "it", "sw" and "fin". Each has more than one reading,
# and an expansion that guesses wrong merges two different roles.

# --- Seniority --------------------------------------------------------------

# Ordered longest-phrase-first so "vice president" wins over "president".
_SENIORITY_PHRASES: tuple[tuple[str, Seniority], ...] = (
    ("vice president", Seniority.EXECUTIVE),
    ("new grad", Seniority.ENTRY),
    ("recent graduate", Seniority.ENTRY),
    ("entry level", Seniority.ENTRY),
    ("early career", Seniority.ENTRY),
    ("head of", Seniority.EXECUTIVE),
    ("internship", Seniority.INTERN),
    ("intern", Seniority.INTERN),
    ("trainee", Seniority.ENTRY),
    ("apprentice", Seniority.INTERN),
    ("graduate", Seniority.ENTRY),
    ("junior", Seniority.JUNIOR),
    ("jr", Seniority.JUNIOR),
    ("associate", Seniority.JUNIOR),
    ("intermediate", Seniority.MID),
    ("senior", Seniority.SENIOR),
    ("sr", Seniority.SENIOR),
    # No STAFF member exists; Staff sits above Senior in the ladders that use
    # it, so it maps to LEAD. Mapping it to None would make "Staff Analyst" and
    # "Senior Analyst" look compatible, which is the one outcome to avoid.
    ("staff", Seniority.LEAD),
    ("principal", Seniority.PRINCIPAL),
    ("lead", Seniority.LEAD),
    ("director", Seniority.DIRECTOR),
    ("manager", Seniority.MANAGER),
    ("mgr", Seniority.MANAGER),
    ("chief", Seniority.EXECUTIVE),
    ("president", Seniority.EXECUTIVE),
    ("vp", Seniority.EXECUTIVE),
)

# Level markers: "Analyst II" and "Analyst III" are different jobs at the same
# company, and merging them would hide one of them entirely.
_ROMAN_LEVELS = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}
_LEVEL_TOKEN = re.compile(r"^(?:i{1,3}|iv|v|[1-5])$", flags=re.IGNORECASE)

#: Titles this similar, with matching seniority and level, are the same role.
TITLE_SIMILARITY_THRESHOLD = 0.75


@dataclass(frozen=True, slots=True)
class NormalizedTitle:
    """A title split into what to show and what to compare on."""

    display: str
    key: str
    seniority: Seniority | None
    level: int | None
    work_mode_hint: WorkMode | None

    @property
    def seniority_rank(self) -> int | None:
        return SENIORITY_RANK[self.seniority] if self.seniority else None


def _strip_noise(title: str) -> str:
    text = _REQUISITION.sub(" ", title)
    text = _BARE_HASH_NUMBER.sub(" ", text)
    text = _GENDER_MARKER.sub(" ", text)
    text = _MARKETING.sub(" ", text)
    text = _EMPTY_BRACKETS.sub(" ", text)
    return text


def detect_work_mode(*values: str | None) -> WorkMode | None:
    """Read a work arrangement out of free text, or return None.

    Never guesses. A posting that does not say how it is worked gets None, and
    the dashboard shows nothing rather than "Onsite" invented on its behalf.
    """
    haystack = comparison_key(" ".join(v for v in values if v))
    if not haystack:
        return None

    padded = f" {haystack} "
    # Hybrid first: "hybrid remote" is hybrid, not remote.
    if " hybrid " in padded or " partially remote " in padded:
        return WorkMode.HYBRID
    if (
        " remote " in padded
        or " wfh " in padded
        or " work from home " in padded
        or " telecommute " in padded
        or " fully remote " in padded
    ):
        return WorkMode.REMOTE
    if " onsite " in padded or " on site " in padded or " in office " in padded:
        return WorkMode.ONSITE

    return None


def extract_seniority(title: str | None) -> Seniority | None:
    """Highest seniority signal present in a title, or None."""
    key = comparison_key(title)
    if not key:
        return None

    padded = f" {key} "
    for phrase, seniority in _SENIORITY_PHRASES:
        if f" {phrase} " in padded:
            return seniority

    return None


def extract_level(title: str | None) -> int | None:
    """Numeric level marker, e.g. 3 for "Financial Analyst III"."""
    title_tokens = comparison_key(title).split()
    if not title_tokens:
        return None

    # Only a trailing marker counts. A leading "I" is far more likely to be a
    # word than a level, and "II" mid-title is usually part of a product name.
    last = title_tokens[-1]
    if not _LEVEL_TOKEN.match(last):
        return None
    # A single "i" is almost always noise rather than "level 1".
    if last == "i":
        return None

    return _ROMAN_LEVELS.get(last, None) or (int(last) if last.isdigit() else None)


def normalize_title(title: str | None) -> NormalizedTitle | None:
    """Normalise one title. Returns None when there is nothing usable left."""
    display = clean_display_text(title)
    if not display:
        return None

    # Noise removal is applied to the display value too: a requisition number
    # is not something a user needs to read in a results table.
    cleaned = clean_display_text(_strip_noise(display)) or display

    seniority = extract_seniority(cleaned)
    level = extract_level(cleaned)
    work_mode_hint = detect_work_mode(cleaned)

    # The key keeps seniority words - two titles differing only by "Senior" are
    # different roles - but drops arrangement and employment qualifiers and
    # expands abbreviations so the same role written two ways still matches.
    key_tokens: list[str] = []
    for token in comparison_key(cleaned).split():
        if token in _QUALIFIER_WORDS:
            continue
        key_tokens.extend(_ABBREVIATIONS.get(token, token).split())

    return NormalizedTitle(
        display=cleaned,
        key=" ".join(key_tokens),
        seniority=seniority,
        level=level,
        work_mode_hint=work_mode_hint,
    )


def titles_conflict(left: NormalizedTitle, right: NormalizedTitle) -> str | None:
    """Return why two titles cannot be the same vacancy, or None.

    Seniority and level are hard blockers. "Senior Financial Analyst" and
    "Junior Financial Analyst" share almost every word, and no amount of
    similarity should let them merge.
    """
    if (
        left.seniority is not None
        and right.seniority is not None
        and left.seniority is not right.seniority
    ):
        return f"seniority differs ({left.seniority.value} vs {right.seniority.value})"

    if left.level is not None and right.level is not None and left.level != right.level:
        return f"level differs ({left.level} vs {right.level})"

    # One title states a level and the other states a different seniority: not
    # enough to call them the same posting.
    if (left.level is None) != (right.level is None):
        stated = left.level if left.level is not None else right.level
        if stated is not None and stated > 1:
            return "one title states a level marker and the other does not"

    return None


def title_equivalence(
    left: NormalizedTitle, right: NormalizedTitle
) -> tuple[bool, float, str]:
    """Are these the same role? Returns (equivalent, similarity, reason)."""
    conflict = titles_conflict(left, right)
    if conflict is not None:
        return False, 0.0, conflict

    if left.key and left.key == right.key:
        return True, 1.0, "identical normalised title"

    score = similarity(left.key, right.key)
    if score >= TITLE_SIMILARITY_THRESHOLD:
        return True, score, f"titles {score:.0%} similar with matching seniority"

    return False, score, f"titles only {score:.0%} similar"
