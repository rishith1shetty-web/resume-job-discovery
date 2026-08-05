"""US eligibility for a collected listing.

Phase 10 hard-locks this product to United States vacancies. This module is the
single place that decides whether a posting is in the US, and it runs during
normalisation - before matching, scoring or deduplication ever see the job.

# The rule that governs everything here

**Never guess.** A listing is accepted only when the location it published
carries positive evidence of the United States. Anything that cannot be
classified with confidence is rejected, including a listing with no location at
all. Rejecting a real US job because it was described vaguely is a cost this
product accepts; showing a user a job in Lisbon is not.

# How the evidence is read

Four kinds of positive evidence, in descending confidence:

* ``EXPLICIT_COUNTRY`` - the location names the country: "United States",
  "USA", "US", "U.S.". This is also what makes a remote role acceptable:
  "Remote USA", "US Remote" and "Remote (United States)" all land here, while a
  bare "Remote" does not.
* ``STATE`` - a state name anywhere in the text, or a state abbreviation
  standing alone as its own component. All 50 states plus DC.
* ``KNOWN_CITY`` - a curated list of large, unambiguous US cities, used only
  when no country and no state was stated.

# Two traps this code exists to avoid

**Two-letter collisions.** Half the US state abbreviations are also ISO country
codes: ``CA`` is California and Canada, ``IN`` is Indiana and India, ``DE`` is
Delaware and Germany, ``MT`` is Montana and Malta. Live Scale.jobs data really
does publish ``{"country": "MT"}`` for a job in Malta. So an abbreviation is
honoured **only when it stands alone as a whole component** and only when the
text carries no non-US country - and adapters are expected to publish country
names rather than bare codes (see ``scalejobs.py``). A bare two-letter code that
arrives with a country name attached loses to the country name.

**Substring accidents.** ``OR`` is Oregon and also the word "or" that portals
use to join places ("Alpharetta or Oklahoma City"); ``IN``, ``ME``, ``HI``,
``OK``, ``LA`` and ``PA`` are all common English fragments. Nothing here does
substring matching on abbreviations: components are split first and compared
whole.

# Ambiguous city names are deliberately absent

Cambridge, Birmingham, Manchester, Richmond, Vancouver, Ontario, Windsor,
Hamilton, Victoria, Sydney, Athens, Naples, Toledo and Alexandria are all real
US places **and** well-known places elsewhere. None of them is in the curated
list, because "Vancouver" alone cannot be told from Vancouver, British
Columbia. They are accepted only when a state or country appears alongside.

# Postings that name several places

A vacancy hiring in "Dallas, Texas, San Francisco, California, Vancouver,
Canada" is a US job that also hires elsewhere. Positive US evidence therefore
wins over a non-US signal, and the reason records that both were seen. The
reverse - "London, England, GB" - has no US evidence and is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: Reason recorded on a rejected capture and surfaced in the run summary.
NON_US_REJECTION = "non_us_location"


class LocationConfidence(StrEnum):
    """How the US verdict was reached."""

    EXPLICIT_COUNTRY = "explicit_country"
    STATE = "state"
    KNOWN_CITY = "known_city"
    NON_US_COUNTRY = "non_us_country"
    NON_US_CITY = "non_us_city"
    UNSPECIFIED_REGION = "unspecified_region"
    NO_LOCATION = "no_location"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class UsLocationVerdict:
    """Whether one posting is a US vacancy, and how that was decided."""

    is_us: bool
    confidence: LocationConfidence
    reason: str
    #: "United States" when accepted; the country that caused a rejection when
    #: one was actually named. None when nothing could be identified.
    country: str | None = None
    #: ISO-3166 alpha-2, only ever "US" here. None when not US.
    country_code: str | None = None


# --- Vocabulary -------------------------------------------------------------

US_COUNTRY_ALIASES = frozenset(
    {
        "united states",
        "united states of america",
        "usa",
        "us",
        "usofa",
        "america",
        "u s",
        "u s a",
    }
)

US_STATE_NAMES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

US_STATE_ABBREVIATIONS = frozenset(value.lower() for value in US_STATE_NAMES.values())

#: Large US cities that are not also well-known cities elsewhere. Used only
#: when neither a state nor a country was stated. Deliberately conservative -
#: see the module docstring for the names left out on purpose.
US_CITIES = frozenset(
    {
        "atlanta",
        "austin",
        "baltimore",
        "baton rouge",
        "bellevue",
        "berkeley",
        "boise",
        "boston",
        "boulder",
        "bronx",
        "brooklyn",
        "buffalo",
        "burbank",
        "cary",
        "charlotte",
        "chattanooga",
        "chicago",
        "cincinnati",
        "cleveland",
        "colorado springs",
        "columbus",
        "corpus christi",
        "cupertino",
        "dallas",
        "denver",
        "des moines",
        "detroit",
        "durham",
        "el paso",
        "fort lauderdale",
        "fort wayne",
        "fort worth",
        "fremont",
        "fresno",
        "grand rapids",
        "greensboro",
        "hartford",
        "honolulu",
        "houston",
        "huntsville",
        "indianapolis",
        "irvine",
        "jacksonville",
        "jersey city",
        "kansas city",
        "knoxville",
        "las vegas",
        "lexington",
        "little rock",
        "long beach",
        "los angeles",
        "louisville",
        "lubbock",
        "madison",
        "manhattan",
        "memphis",
        "menlo park",
        "mesa",
        "miami",
        "milwaukee",
        "minneapolis",
        "mountain view",
        "nashville",
        "new orleans",
        "new york",
        "new york city",
        "newark",
        "nyc",
        "oakland",
        "oklahoma city",
        "omaha",
        "orlando",
        "overland park",
        "palo alto",
        "pasadena",
        "philadelphia",
        "phoenix",
        "pittsburgh",
        "plano",
        "portland",
        "providence",
        "queens",
        "raleigh",
        "redmond",
        "reno",
        "sacramento",
        "saint louis",
        "saint paul",
        "salt lake city",
        "san antonio",
        "san diego",
        "san francisco",
        "san jose",
        "santa clara",
        "santa monica",
        "scottsdale",
        "seattle",
        "spokane",
        "st louis",
        "st paul",
        "stamford",
        "sunnyvale",
        "tampa",
        "tempe",
        "tucson",
        "tulsa",
        "virginia beach",
        "wichita",
        "winston salem",
    }
)

#: Countries and territories that are not the United States. A name here is
#: decisive only when no US evidence was found.
NON_US_COUNTRIES = frozenset(
    {
        "afghanistan", "albania", "algeria", "andorra", "angola", "argentina",
        "armenia", "australia", "austria", "azerbaijan", "bahrain",
        "bangladesh", "belarus", "belgium", "bolivia", "bosnia", "botswana",
        "brazil", "bulgaria", "cambodia", "cameroon", "canada", "chile",
        "china", "colombia", "costa rica", "croatia", "cyprus", "czechia",
        "czech republic", "denmark", "dominican republic", "ecuador", "egypt",
        "el salvador", "estonia", "ethiopia", "finland", "france",
        "germany", "ghana", "gibraltar", "great britain", "greece", "guatemala",
        "honduras", "hong kong", "hungary", "iceland", "india", "indonesia",
        "iraq", "ireland", "israel", "italy", "jamaica", "japan", "jordan",
        "kazakhstan", "kenya", "kuwait", "latvia", "lebanon", "lithuania",
        "luxembourg", "macau", "malaysia", "malta", "mexico", "moldova",
        "monaco", "mongolia", "montenegro", "morocco", "myanmar", "nepal",
        "netherlands", "new zealand", "nicaragua", "nigeria", "north macedonia",
        "norway", "oman", "pakistan", "panama", "paraguay", "peru",
        "philippines", "poland", "portugal", "qatar", "romania", "russia",
        "rwanda", "san marino", "saudi arabia", "senegal", "serbia",
        "singapore", "slovakia", "slovenia", "south africa", "south korea",
        "korea", "spain", "sri lanka", "sweden", "switzerland", "taiwan",
        "tanzania", "thailand", "tunisia", "turkey", "turkiye", "uganda",
        "ukraine", "united arab emirates", "uae", "united kingdom", "uk",
        "uruguay", "uzbekistan", "venezuela", "vietnam", "zambia", "zimbabwe",
        "scotland", "wales", "northern ireland", "england", "britain",
        "bosnia and herzegovina", "cayman islands", "gabon", "laos",
        "montserrat", "new caledonia", "niger", "seychelles", "sudan",
        "vatican city", "liechtenstein", "malawi", "mozambique", "namibia",
        "bahamas", "barbados", "trinidad and tobago", "fiji", "papua new guinea",
    }
)

#: Phrases that settle a country whose name collides with a US state name, and
#: which therefore have to be checked before the state rule. "Georgia" is the
#: only real case: it is a US state and a country, and the state is what job
#: postings almost always mean, so the country has to identify itself.
NON_US_PHRASES: tuple[str, ...] = (
    "georgia (country)",
    "republic of georgia",
)
# Deliberately absent above: Puerto Rico, Guam and the other US territories.
# They are not among the 50 states plus DC that this phase accepts, but calling
# them "not the United States" would be false. They fall through to
# UNCLASSIFIED and are rejected with an honest reason instead.

# Also absent: "Georgia". It is a country and a US state, and in job postings
# the state is overwhelmingly the intended one, so it is read as Alabama's
# neighbour. A posting for Tbilisi that says only "Georgia" would be accepted
# in error; one that says "Georgia, Tbilisi" or names the country alongside a
# European city is not, because the city list catches it.

#: Cities that are decisive evidence of somewhere other than the US.
NON_US_CITIES = frozenset(
    {
        "amsterdam", "bangalore", "bengaluru", "barcelona", "beijing",
        "belfast", "belgrade", "berlin", "bogota", "bratislava", "brisbane",
        "brussels", "bucharest", "budapest", "buenos aires", "cairo",
        "cape town", "chennai", "copenhagen", "delhi", "new delhi", "dubai",
        "dublin", "dusseldorf", "edinburgh", "frankfurt", "geneva", "glasgow",
        "gurgaon", "gurugram", "hanoi", "helsinki", "ho chi minh city",
        "hyderabad", "istanbul", "jakarta", "johannesburg", "karachi", "kiev",
        "kyiv", "kolkata", "kuala lumpur", "lagos", "lahore", "leeds", "lima",
        "lisbon", "lisboa", "ljubljana", "london", "madrid", "manila",
        "marseille", "medellin", "melbourne", "mexico city", "milan", "milano",
        "montreal", "moscow", "mumbai", "munich", "nairobi", "noida", "osaka",
        "oslo", "ottawa", "paris", "porto", "prague", "pune", "reykjavik",
        "riga", "rio de janeiro", "riyadh", "rome", "roma", "rotterdam",
        "santiago", "sao paulo", "seoul", "shanghai", "shenzhen", "sofia",
        "tbilisi", "yerevan", "baku", "minsk", "almaty", "tashkent",
        "stockholm", "stuttgart", "taipei", "tallinn", "tel aviv", "tokyo",
        "toronto", "valletta", "vienna", "vilnius", "warsaw", "wellington",
        "zagreb", "zurich", "calgary", "edmonton", "winnipeg", "quebec",
        "quebec city", "auckland", "perth", "adelaide", "cork", "galway",
        "leon", "guadalajara", "monterrey", "bogotá",
    }
)

#: ISO-3166 alpha-2 to country name, for adapters whose upstream publishes a
#: code rather than a name.
#:
#: This exists because a bare code is genuinely ambiguous against US state
#: abbreviations - ``CA``, ``IN``, ``DE``, ``MT``, ``MA``, ``MD``, ``PA`` and a
#: dozen more mean one thing to ISO and another to the postal service. An
#: adapter that knows a field *is* a country resolves it here, so the
#: classifier never has to guess. Every code that collides with a US state
#: abbreviation is present; unknown codes are left to the caller to handle.
COUNTRY_NAMES_BY_CODE: dict[str, str] = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan",
    "AL": "Albania", "AM": "Armenia", "AO": "Angola", "AR": "Argentina",
    "AT": "Austria", "AU": "Australia", "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina", "BD": "Bangladesh", "BE": "Belgium",
    "BG": "Bulgaria", "BH": "Bahrain", "BO": "Bolivia", "BR": "Brazil",
    "BW": "Botswana", "BY": "Belarus", "CA": "Canada", "CH": "Switzerland",
    "CL": "Chile", "CM": "Cameroon", "CN": "China", "CO": "Colombia",
    "CR": "Costa Rica", "CY": "Cyprus", "CZ": "Czechia", "DE": "Germany",
    "DK": "Denmark", "DO": "Dominican Republic", "DZ": "Algeria",
    "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt", "ES": "Spain",
    "ET": "Ethiopia", "FI": "Finland", "FR": "France", "GA": "Gabon",
    "GB": "United Kingdom",
    "GE": "Georgia (country)", "GH": "Ghana", "GI": "Gibraltar",
    "GR": "Greece", "GT": "Guatemala", "HK": "Hong Kong", "HN": "Honduras",
    "HR": "Croatia", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland",
    "IL": "Israel", "IN": "India", "IQ": "Iraq", "IS": "Iceland",
    "IT": "Italy", "JM": "Jamaica", "JO": "Jordan", "JP": "Japan",
    "KE": "Kenya", "KH": "Cambodia", "KR": "South Korea", "KW": "Kuwait",
    "KY": "Cayman Islands",
    "KZ": "Kazakhstan", "LA": "Laos", "LB": "Lebanon", "LK": "Sri Lanka",
    "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "MA": "Morocco",
    "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro", "MK": "North Macedonia",
    "MN": "Mongolia", "MO": "Macau", "MS": "Montserrat", "MT": "Malta",
    "MX": "Mexico", "MY": "Malaysia", "MM": "Myanmar", "NC": "New Caledonia",
    "NE": "Niger",
    "NG": "Nigeria", "NI": "Nicaragua", "NL": "Netherlands", "NO": "Norway",
    "NP": "Nepal", "NZ": "New Zealand", "OM": "Oman", "PA": "Panama",
    "PE": "Peru", "PH": "Philippines", "PK": "Pakistan", "PL": "Poland",
    "PT": "Portugal", "PY": "Paraguay", "QA": "Qatar", "RO": "Romania",
    "RS": "Serbia", "RU": "Russia", "RW": "Rwanda", "SA": "Saudi Arabia",
    "SC": "Seychelles", "SD": "Sudan", "SE": "Sweden", "SG": "Singapore",
    "SI": "Slovenia", "SK": "Slovakia", "SM": "San Marino", "SN": "Senegal",
    "SV": "El Salvador", "TH": "Thailand", "TN": "Tunisia", "TR": "Turkey",
    "TW": "Taiwan", "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda",
    "US": "United States", "UY": "Uruguay", "UZ": "Uzbekistan",
    "VA": "Vatican City", "VE": "Venezuela", "VN": "Vietnam",
    "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}


#: Places that name no country at all. Never enough on their own.
UNSPECIFIED_REGIONS = frozenset(
    {
        "remote", "worldwide", "global", "globally", "anywhere",
        "anywhere in the world", "emea", "apac", "latam", "amer", "eu",
        "europe", "european union", "asia", "asia pacific", "africa",
        "middle east", "north america", "south america", "latin america",
        "international", "multiple locations", "various locations",
        "work from home", "wfh", "virtual", "telecommute", "distributed",
        "flexible", "nationwide",
    }
)

# Separators portals put between the parts of a location. A spaced hyphen is a
# separator ("Remote - California"); an unspaced one is part of a name
# ("Winston-Salem"), so it is left alone.
_SPLIT = re.compile(r"[,;|/()\[\]\n]|\s+[-–—]\s+")
#: Portals join alternative places with "or". Word-bounded, so it cannot fire
#: inside "Maryland" or match the Oregon abbreviation standing alone.
_OR = re.compile(r"\bor\b")
_PUNCT = re.compile(r"[.’']")
_WHITESPACE = re.compile(r"\s+")

_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(n) for n in US_STATE_NAMES), key=len, reverse=True)) + r")\b"
)


def _normalise(value: str) -> str:
    """Lowercase, drop periods and apostrophes, collapse whitespace.

    Periods go so "U.S." reads as "us" and "D.C." as "dc"; both are the forms
    the rest of this module compares against.
    """
    return _WHITESPACE.sub(" ", _PUNCT.sub("", value.casefold())).strip()


def _components(text: str) -> list[str]:
    """The comma-level parts of a location, normalised and non-empty."""
    return [part for raw in _SPLIT.split(text) if (part := _normalise(raw))]


#: Arrangement words that sit next to a place without being one. Portals write
#: "Remote US", "US Remote" and "Remote - India" interchangeably, so these are
#: peeled off the ends of a component to expose the place underneath.
_MODIFIERS = (
    "remote",
    "hybrid",
    "onsite",
    "on site",
    "work from home",
    "wfh",
    "virtual",
    "telecommute",
    "based",
    "only",
    "area",
    "metro",
    "region",
    "greater",
)


def _strip_modifiers(component: str) -> str:
    """Peel arrangement words off both ends of a component.

    "remote us" becomes "us" and "remote canada" becomes "canada", which is
    what lets rule 4 accept "Remote USA" while still rejecting a bare "Remote".
    Only whole leading or trailing words are removed, so a place that merely
    contains one of these words is untouched.
    """
    changed = True
    while changed:
        changed = False
        for word in _MODIFIERS:
            for candidate in (f"{word} ", f" {word}"):
                if candidate.startswith(" ") and component.endswith(candidate):
                    component = component[: -len(candidate)].strip()
                    changed = True
                elif not candidate.startswith(" ") and component.startswith(candidate):
                    component = component[len(candidate) :].strip()
                    changed = True
    return component


def _subcomponents(components: list[str]) -> list[str]:
    """Components split on the joining word "or", and stripped of arrangement words.

    "Portland, OR" must keep "or" as its own component while "Alpharetta or
    Oklahoma City" must not produce one, which is why the split is word-bounded
    and applied here rather than to the raw text.
    """
    out: list[str] = []
    for component in components:
        for piece in _OR.split(component):
            if cleaned := _normalise(piece):
                out.append(cleaned)
                if (bare := _strip_modifiers(cleaned)) and bare != cleaned:
                    out.append(bare)
    return out


def classify_us_location(location: str | None) -> UsLocationVerdict:
    """Decide whether a posting's location is in the United States.

    Reads only the location the portal published. Titles and descriptions are
    deliberately not mined for a country: "US GAAP Analyst" is a finance title,
    not a statement that the role is in the United States, and treating it as
    one would be exactly the guess this phase forbids.
    """
    text = (location or "").strip()
    if not text:
        return UsLocationVerdict(
            is_us=False,
            confidence=LocationConfidence.NO_LOCATION,
            reason="the listing published no location",
        )

    normalised = _normalise(text)
    components = _components(text)
    pieces = _subcomponents(components)
    searchable = set(components) | set(pieces)

    # --- Negative evidence, gathered first so it can be reported ------------
    countries = sorted(searchable & NON_US_COUNTRIES)
    cities = sorted(searchable & NON_US_CITIES)

    # A country that names itself to distinguish it from the US state of the
    # same name is decisive, and has to be read before the state rule.
    for phrase in NON_US_PHRASES:
        if phrase in normalised:
            return UsLocationVerdict(
                is_us=False,
                confidence=LocationConfidence.NON_US_COUNTRY,
                reason=f"names {phrase!r}, which is not the United States",
                country=phrase,
            )

    # --- Positive evidence -------------------------------------------------
    if searchable & US_COUNTRY_ALIASES:
        return _accepted(
            LocationConfidence.EXPLICIT_COUNTRY,
            "the location names the United States",
            countries,
            cities,
        )

    state_match = _STATE_NAME_RE.search(normalised)
    if state_match:
        return _accepted(
            LocationConfidence.STATE,
            f"names the US state {state_match.group(1)!r}",
            countries,
            cities,
        )

    # A bare two-letter code is only a state when nothing contradicts it. Half
    # of them are also ISO country codes, so a named foreign country wins.
    candidates = set(components) | {_strip_modifiers(part) for part in components}
    abbreviations = sorted(candidates & US_STATE_ABBREVIATIONS)
    if abbreviations and not countries and not cities:
        return _accepted(
            LocationConfidence.STATE,
            f"names the US state abbreviation {abbreviations[0].upper()!r}",
            countries,
            cities,
        )

    if countries:
        return UsLocationVerdict(
            is_us=False,
            confidence=LocationConfidence.NON_US_COUNTRY,
            reason=f"names {countries[0]!r}, which is not the United States",
            country=countries[0],
        )

    if cities:
        return UsLocationVerdict(
            is_us=False,
            confidence=LocationConfidence.NON_US_CITY,
            reason=f"names {cities[0]!r}, which is not in the United States",
        )

    known = sorted(searchable & US_CITIES)
    if known:
        return UsLocationVerdict(
            is_us=True,
            confidence=LocationConfidence.KNOWN_CITY,
            reason=f"names the US city {known[0]!r}",
            country="United States",
            country_code="US",
        )

    if searchable & UNSPECIFIED_REGIONS:
        return UsLocationVerdict(
            is_us=False,
            confidence=LocationConfidence.UNSPECIFIED_REGION,
            reason=(
                f"{text!r} names no country; a remote or region-wide listing is "
                "accepted only when it says the United States"
            ),
        )

    return UsLocationVerdict(
        is_us=False,
        confidence=LocationConfidence.UNCLASSIFIED,
        reason=f"{text!r} could not be confidently placed in the United States",
    )


def _accepted(
    confidence: LocationConfidence,
    reason: str,
    countries: list[str],
    cities: list[str],
) -> UsLocationVerdict:
    """Build an acceptance, noting any other country the posting also listed."""
    elsewhere = countries or cities
    if elsewhere:
        reason = f"{reason}; also lists {elsewhere[0]!r}, so it hires in the US and elsewhere"
    return UsLocationVerdict(
        is_us=True,
        confidence=confidence,
        reason=reason,
        country="United States",
        country_code="US",
    )


__all__ = [
    "COUNTRY_NAMES_BY_CODE",
    "NON_US_CITIES",
    "NON_US_COUNTRIES",
    "NON_US_REJECTION",
    "UNSPECIFIED_REGIONS",
    "US_CITIES",
    "US_COUNTRY_ALIASES",
    "US_STATE_ABBREVIATIONS",
    "US_STATE_NAMES",
    "LocationConfidence",
    "UsLocationVerdict",
    "classify_us_location",
]
