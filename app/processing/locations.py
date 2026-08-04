"""Location normalisation and work-mode detection.

The hard rule (CLAUDE.md, Phase 5 brief): **do not invent city, state or
country values**. A posting that says "Boston" gets a location of "Boston", not
"Boston, MA, United States". US state names are folded to their two-letter
abbreviation for comparison only, because "Boston, Massachusetts" and
"Boston, MA" are the same string written twice - that is a spelling, not an
inference.

Locations are also the field most likely to *stop* a merge. Two identical
"Financial Analyst" postings at the same employer in Boston and Denver are two
vacancies, and treating them as one hides a job the user could apply for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.constants import WorkMode
from app.processing.text import clean_display_text, comparison_key
from app.processing.titles import detect_work_mode

# Separator variants portals use between the parts of a location.
_SEPARATORS = re.compile(r"\s*[|/;–—]\s*|\s*,\s*")
_REPEATED_COMMA = re.compile(r"(?:,\s*){2,}")

# Country names dropped from the comparison key only. "Boston, MA" and
# "Boston, MA, United States" are the same place.
_COUNTRY_ALIASES = frozenset(
    {
        "united states",
        "united states of america",
        "usa",
        "us",
        "u s",
        "u s a",
        "america",
        "united kingdom",
        "uk",
        "u k",
        "great britain",
        "canada",
        "india",
        "australia",
        "germany",
        "france",
        "netherlands",
        "ireland",
        "singapore",
    }
)

# Words that describe an arrangement rather than a place.
_MODE_WORDS = frozenset(
    {
        "remote",
        "hybrid",
        "onsite",
        "on site",
        "work from home",
        "wfh",
        "telecommute",
        "virtual",
        "anywhere",
        "nationwide",
        "multiple locations",
        "various locations",
        "flexible",
    }
)

_US_STATES = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "district of columbia": "dc",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
}


@dataclass(frozen=True, slots=True)
class NormalizedLocation:
    """A location split into display text, a comparison key and a work mode."""

    display: str | None
    key: str
    work_mode: WorkMode | None
    #: The place parts only, with arrangement and country words removed.
    parts: tuple[str, ...]

    @property
    def is_placeless(self) -> bool:
        """True when the posting named an arrangement but no actual place."""
        return not self.parts


def _canonical_part(part: str, *, position: int) -> str | None:
    """Canonicalise one comma-separated component.

    State folding is applied from the second component onwards only. Several
    US cities share a name with a state - New York, Washington, Oklahoma City -
    and folding the leading component would turn "New York, NY" into "ny ny",
    which then looks like a less specific form of "Buffalo, NY".
    """
    key = comparison_key(part)
    if not key or key in _COUNTRY_ALIASES or key in _MODE_WORDS:
        return None
    if position == 0:
        return key
    return _US_STATES.get(key, key)


def normalize_location(
    location: str | None, *, extra_mode_text: str | None = None
) -> NormalizedLocation:
    """Normalise a location string.

    ``extra_mode_text`` lets the caller pass the title or description so
    "Remote" stated in the title still sets the work mode when the location
    field only names a city.
    """
    display = clean_display_text(location)
    if display:
        # Normalise separators to ", " without reordering or adding anything.
        display = _SEPARATORS.sub(", ", display)
        display = _REPEATED_COMMA.sub(", ", display)
        display = clean_display_text(display)

    work_mode = detect_work_mode(location, extra_mode_text)

    parts: tuple[str, ...] = ()
    if display:
        parts = tuple(
            canonical
            for index, raw_part in enumerate(display.split(","))
            if (canonical := _canonical_part(raw_part, position=index)) is not None
        )

    return NormalizedLocation(
        display=display,
        key=" ".join(parts),
        work_mode=work_mode,
        parts=parts,
    )


def locations_compatible(
    left: NormalizedLocation, right: NormalizedLocation
) -> tuple[bool, str]:
    """Could these two postings be in the same place? Returns (compatible, reason).

    Compatible is a weaker claim than equal: an unknown location on one side,
    or a remote role on either, does not rule a merge out. A concrete
    disagreement between two on-site places does.
    """
    if not left.parts or not right.parts:
        return True, "location unknown on one side"

    if left.key == right.key:
        return True, "identical location"

    # One side names a city and a state, the other only the city. Compatible as
    # long as everything the shorter one says is also said by the longer.
    left_set, right_set = set(left.parts), set(right.parts)
    if left_set <= right_set or right_set <= left_set:
        return True, "one location is a less specific form of the other"

    # Two remote postings naming different hiring hubs are still one remote
    # role. This does not extend to a remote posting versus an on-site one:
    # those are two vacancies with different working arrangements.
    if left.work_mode is WorkMode.REMOTE and right.work_mode is WorkMode.REMOTE:
        return True, "both postings are remote, listing different hiring regions"

    # Sharing only a state is not enough. Buffalo and New York City are both
    # "NY", and merging them would hide one of two real vacancies.
    return False, f"locations conflict ({left.key!r} vs {right.key!r})"
