"""US eligibility: what is accepted, what is rejected, and what is never guessed.

Phase 10 locks this product to United States vacancies. These tests cover the
rules directly, then the traps that make the rules hard: two-letter codes that
are both a US state and a country, joining words that look like abbreviations,
and postings that name several places at once.
"""

from __future__ import annotations

import pytest

from app.processing.us_locations import (
    COUNTRY_NAMES_BY_CODE,
    NON_US_CITIES,
    NON_US_COUNTRIES,
    US_CITIES,
    US_STATE_ABBREVIATIONS,
    US_STATE_NAMES,
    LocationConfidence,
    classify_us_location,
)


def accepted(value: str | None) -> bool:
    return classify_us_location(value).is_us


# --- Rule 1: explicit country ----------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Austin, United States",
        "Austin, USA",
        "Austin, US",
        "Austin, U.S.",
        "Austin, U.S.A.",
        "United States",
        "United States of America",
        "Boston, MA, USA",
        "Chicago, IL, USA, New York, NY, USA, San Francisco, CA, USA",
    ],
)
def test_an_explicit_united_states_is_accepted(value: str) -> None:
    verdict = classify_us_location(value)

    assert verdict.is_us
    assert verdict.confidence is LocationConfidence.EXPLICIT_COUNTRY
    assert verdict.country == "United States"
    assert verdict.country_code == "US"


# --- Rule 2: all 50 states plus DC -----------------------------------------


@pytest.mark.parametrize("state", sorted(US_STATE_NAMES))
def test_every_state_name_is_accepted(state: str) -> None:
    verdict = classify_us_location(f"Springfield, {state.title()}")

    assert verdict.is_us, state
    assert verdict.confidence is LocationConfidence.STATE


@pytest.mark.parametrize("abbreviation", sorted(US_STATE_ABBREVIATIONS))
def test_every_state_abbreviation_is_accepted(abbreviation: str) -> None:
    verdict = classify_us_location(f"Springfield, {abbreviation.upper()}")

    assert verdict.is_us, abbreviation
    assert verdict.confidence is LocationConfidence.STATE


def test_all_fifty_states_and_dc_are_covered() -> None:
    assert len(US_STATE_NAMES) == 51
    assert "district of columbia" in US_STATE_NAMES
    assert len(US_STATE_ABBREVIATIONS) == 51


@pytest.mark.parametrize(
    "value",
    [
        "Washington, D.C.",
        "Maryland, Virginia, Washington, D.C.",
        "California State",
        "New York City, New York",
        "Remote - California",
        "Bellevue, WA, Menlo Park, CA",
    ],
)
def test_states_are_found_however_they_are_written(value: str) -> None:
    assert accepted(value), value


# --- Rule 3: curated cities -------------------------------------------------


@pytest.mark.parametrize(
    "city",
    [
        "Seattle", "Chicago", "Austin", "Boston", "Dallas", "Houston",
        "San Francisco", "Los Angeles", "Miami", "Phoenix", "Denver",
        "Atlanta", "Nashville", "Detroit", "Philadelphia", "Charlotte",
        "Portland", "Pittsburgh", "Baltimore", "Indianapolis", "Cincinnati",
        "Columbus", "Kansas City", "Las Vegas", "Minneapolis", "Orlando",
    ],
)
def test_curated_cities_are_accepted_without_a_state(city: str) -> None:
    """Accepted on the city alone - or on a state its name contains.

    "Kansas City" and "Oklahoma City" carry a state name inside them, so they
    are accepted by the stronger rule. Either way the vacancy is in the US,
    which is what this asserts.
    """
    verdict = classify_us_location(city)

    assert verdict.is_us, city
    assert verdict.confidence in {
        LocationConfidence.KNOWN_CITY,
        LocationConfidence.STATE,
    }


@pytest.mark.parametrize(
    "city", ["Seattle", "Boston", "Denver", "Philadelphia", "Pittsburgh"]
)
def test_a_city_with_no_state_in_its_name_is_accepted_as_a_city(city: str) -> None:
    assert classify_us_location(city).confidence is LocationConfidence.KNOWN_CITY


def test_a_city_is_only_consulted_when_no_state_was_stated() -> None:
    """A stated state is stronger evidence and is reported as such."""
    assert classify_us_location("Austin, TX").confidence is LocationConfidence.STATE


@pytest.mark.parametrize(
    "ambiguous",
    ["Cambridge", "Birmingham", "Manchester", "Richmond", "Vancouver",
     "Ontario", "Windsor", "Hamilton", "Victoria", "Athens", "Naples",
     "Toledo", "Alexandria"],
)
def test_cities_that_exist_on_two_continents_are_not_curated(ambiguous: str) -> None:
    """These are real US places and real places elsewhere. Never assumed."""
    assert ambiguous.casefold() not in US_CITIES
    assert not accepted(ambiguous), ambiguous


def test_an_ambiguous_city_is_accepted_once_a_state_is_named() -> None:
    assert accepted("Cambridge, MA")
    assert accepted("Vancouver, Washington")
    assert not accepted("Vancouver, Canada")


# --- Rule 4: remote ---------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["Remote USA", "US Remote", "Remote (USA)", "Remote United States",
     "Remote US", "Remote - United States", "Remote, USA", "USA Remote"],
)
def test_remote_is_accepted_when_it_says_the_united_states(value: str) -> None:
    assert accepted(value), value


@pytest.mark.parametrize(
    "value",
    ["Remote", "Worldwide", "Global", "Anywhere", "EMEA", "APAC", "LATAM",
     "Europe", "International", "Remote - Worldwide", "Work from home"],
)
def test_a_placeless_remote_listing_is_rejected(value: str) -> None:
    verdict = classify_us_location(value)

    assert not verdict.is_us, value
    assert verdict.confidence is LocationConfidence.UNSPECIFIED_REGION


@pytest.mark.parametrize("value", ["Remote Canada", "Remote - India", "Remote UK"])
def test_remote_somewhere_else_is_rejected(value: str) -> None:
    assert not accepted(value), value


# --- Rule 5: non-US ---------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["Toronto, Canada", "London, United Kingdom", "Dublin, Ireland",
     "Berlin, Germany", "Paris, France", "Madrid, Spain", "Lisbon, Portugal",
     "Milan, Italy", "Amsterdam, Netherlands", "Brussels, Belgium",
     "Sydney, Australia", "Melbourne, Australia", "Tokyo, Japan",
     "Bengaluru, India", "Singapore", "Hong Kong", "Mexico City, Mexico",
     "Sao Paulo, Brazil", "Belgrade, Serbia", "Seoul, South Korea"],
)
def test_named_non_us_countries_are_rejected(value: str) -> None:
    verdict = classify_us_location(value)

    assert not verdict.is_us, value
    assert verdict.confidence in {
        LocationConfidence.NON_US_COUNTRY,
        LocationConfidence.NON_US_CITY,
    }


@pytest.mark.parametrize(
    "city",
    ["London", "Toronto", "Paris", "Tokyo", "Sydney", "Melbourne", "Dublin",
     "Berlin", "Amsterdam", "Mumbai", "Montreal", "Zurich"],
)
def test_obvious_non_us_cities_are_rejected_without_a_country(city: str) -> None:
    assert not accepted(city), city


def test_a_rejection_names_the_country_that_caused_it() -> None:
    verdict = classify_us_location("Amsterdam, Netherlands")

    assert verdict.country == "netherlands"
    assert "netherlands" in verdict.reason


# --- Rule 6: never guess ----------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_listing_with_no_location_is_rejected(value: str | None) -> None:
    verdict = classify_us_location(value)

    assert not verdict.is_us
    assert verdict.confidence is LocationConfidence.NO_LOCATION


@pytest.mark.parametrize(
    "value",
    ["Buford or Katy or Port Reading or Fontana", "Head Office", "TBD",
     "Various", "Zone 4", "ZZ"],
)
def test_an_unrecognisable_location_is_rejected(value: str) -> None:
    """Rejecting a real US job described vaguely is the accepted cost."""
    assert not accepted(value), value


# --- The two-letter trap ----------------------------------------------------


@pytest.mark.parametrize(
    ("code", "country"),
    [("CA", "Canada"), ("IN", "India"), ("DE", "Germany"), ("MT", "Malta"),
     ("MA", "Morocco"), ("MD", "Moldova"), ("PA", "Panama"), ("LA", "Laos"),
     ("ME", "Montenegro"), ("NE", "Niger"), ("SC", "Seychelles"),
     ("ID", "Indonesia"), ("IL", "Israel"), ("MO", "Macau"), ("AL", "Albania")],
)
def test_a_named_country_beats_the_state_abbreviation_it_collides_with(
    code: str, country: str
) -> None:
    """``MT`` is Montana and Malta. The spelled-out country decides."""
    assert not accepted(f"Valletta, {country}"), country
    assert COUNTRY_NAMES_BY_CODE[code] == country


@pytest.mark.parametrize(
    "code", sorted(set(COUNTRY_NAMES_BY_CODE) - {"US"} & {c.upper() for c in US_STATE_ABBREVIATIONS})
)
def test_every_colliding_code_resolves_to_a_country_the_filter_rejects(
    code: str,
) -> None:
    """The map must cover every code that could be read as a state."""
    name = COUNTRY_NAMES_BY_CODE[code]

    assert not accepted(name), f"{code} -> {name}"


def test_a_bare_two_letter_code_is_read_as_a_state() -> None:
    """Documented limitation, and why adapters resolve codes to names.

    Nothing in a bare "MT" says whether it came from a postal address or an
    ISO country field, so it is read as Montana. Adapters that know the field
    is a country resolve it before this point - see ``scalejobs.py``.
    """
    assert accepted("MT")
    assert not accepted("Malta")


def test_a_non_us_city_still_wins_against_a_trailing_code() -> None:
    """Defence in depth for the case above."""
    assert not accepted("Valletta, Valletta, MT")
    assert not accepted("Lisbon, Lisbon, PT")


# --- The joining-word trap --------------------------------------------------


def test_the_word_or_is_not_read_as_oregon() -> None:
    verdict = classify_us_location("Alpharetta or Oklahoma City")

    assert verdict.is_us
    # Accepted for naming Oklahoma, not for the word "or".
    assert "oklahoma" in verdict.reason


def test_oregon_still_works_as_its_own_component() -> None:
    assert accepted("Portland, OR")
    assert accepted("Eugene, OR")


@pytest.mark.parametrize(
    "value",
    ["Maryland", "Indiana", "Maine", "Hawaii", "Oklahoma", "Louisiana"],
)
def test_state_names_containing_short_words_are_unharmed(value: str) -> None:
    assert accepted(value), value


def test_india_is_not_read_as_indiana() -> None:
    assert accepted("Indianapolis, Indiana")
    assert not accepted("Bengaluru, India")


# --- Postings naming several places -----------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Dallas, Texas, San Francisco, California, Vancouver, Canada",
        "New York, San Francisco, Seattle, or Remote (US, Canada)",
        "United States or Canada",
        "Paris or New York City or Boston",
    ],
)
def test_a_posting_hiring_in_the_us_and_elsewhere_is_accepted(value: str) -> None:
    """It is a US job that also hires abroad. Rejecting it loses a real job."""
    verdict = classify_us_location(value)

    assert verdict.is_us, value
    assert "also lists" in verdict.reason


def test_a_posting_with_no_us_option_is_rejected_however_many_places_it_names() -> None:
    assert not accepted("London or Paris or Berlin or Dublin")
    assert not accepted("Canada - Remote (ON, AB, BC, or NS Only)")


# --- Vocabulary sanity ------------------------------------------------------


def test_no_city_is_claimed_by_both_lists() -> None:
    assert not (US_CITIES & NON_US_CITIES)


def test_no_us_state_name_is_listed_as_a_foreign_country() -> None:
    assert not (set(US_STATE_NAMES) & NON_US_COUNTRIES)


@pytest.mark.parametrize(
    "code", sorted(c for c in COUNTRY_NAMES_BY_CODE if c != "US")
)
def test_no_country_name_in_the_iso_map_is_read_as_the_united_states(
    code: str,
) -> None:
    """An adapter resolving a code must never hand back something accepted."""
    name = COUNTRY_NAMES_BY_CODE[code]

    assert not accepted(name), f"{code} -> {name}"


def test_the_country_georgia_is_told_apart_from_the_state() -> None:
    """The state is what postings mean; the country has to say so."""
    assert accepted("Atlanta, Georgia")
    assert accepted("Georgia")
    assert not accepted("Georgia (country)")
    assert not accepted("Republic of Georgia")
    assert not accepted("Tbilisi, Georgia (country)")
    assert not accepted("Tbilisi")


def test_the_united_states_is_not_in_the_rejection_list() -> None:
    assert "united states" not in NON_US_COUNTRIES
    assert COUNTRY_NAMES_BY_CODE["US"] == "United States"


def test_us_territories_are_not_called_foreign_countries() -> None:
    """Puerto Rico is not a state, but it is not another country either.

    It falls through to an honest "could not place" rather than being labelled
    as somewhere it is not.
    """
    verdict = classify_us_location("San Juan, Puerto Rico")

    assert not verdict.is_us
    assert verdict.confidence is LocationConfidence.UNCLASSIFIED
