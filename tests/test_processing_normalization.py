"""Field normalisation: titles, companies, locations, dates and text.

No database and no network. Every test here is about one rule from the Phase 5
brief, and most of them are about a rule that says *do not* do something:
do not destroy display text, do not invent a location, do not merge two
employers that only look alike.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.constants import Seniority, WorkMode
from app.processing.companies import companies_match, normalize_company
from app.processing.locations import locations_compatible, normalize_location
from app.processing.normalize import (
    EARLIEST_PLAUSIBLE_POSTING,
    normalize_employment_type,
    normalize_posted_at,
)
from app.processing.text import (
    clean_block_text,
    clean_display_text,
    comparison_key,
    fingerprint,
    similarity,
)
from app.processing.titles import (
    detect_work_mode,
    extract_level,
    extract_seniority,
    normalize_title,
    title_equivalence,
)

# --- Text primitives --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Financial   Analyst  ", "Financial Analyst"),
        ("Financial Analyst\n\tII", "Financial Analyst II"),
        ("-- Financial Analyst --", "Financial Analyst"),
        ("Financial Analyst", "Financial Analyst"),
        ("​Financial Analyst‏", "Financial Analyst"),
        ("Financial --- Analyst", "Financial - Analyst"),
        ("   ", None),
        (None, None),
    ],
)
def test_display_text_is_tidied_without_being_rewritten(
    raw: str | None, expected: str | None
) -> None:
    assert clean_display_text(raw) == expected


def test_display_cleaning_preserves_case_and_wording() -> None:
    # Normalisation is not editing. The portal's words reach the user intact.
    assert clean_display_text("Senior FP&A Analyst") == "Senior FP&A Analyst"
    assert clean_display_text("VP, Corporate Finance") == "VP, Corporate Finance"


def test_block_text_keeps_paragraphs_and_bullets() -> None:
    # A description flattened to one line loses the requirements list, which is
    # exactly what Phase 6 has to read.
    description = "Responsibilities:\n\n- Build models   \n- Report monthly\n\n\n\nRequirements:\n- CFA"

    cleaned = clean_block_text(description)

    assert cleaned is not None
    assert "\n- Build models\n" in cleaned
    assert "\n\n\n" not in cleaned
    assert cleaned.count("\n\n") == 2


def test_comparison_key_folds_case_accents_and_punctuation() -> None:
    assert comparison_key("Sr. Financial Analyst, Inc.") == "sr financial analyst inc"
    assert comparison_key("Zürich") == comparison_key("Zurich")


def test_similarity_is_symmetric_and_bounded() -> None:
    assert similarity("Financial Analyst", "Financial Analyst") == 1.0
    assert similarity("Financial Analyst", None) == 0.0
    assert similarity("a b", "b a") == similarity("b a", "a b")


def test_short_text_is_not_fingerprinted() -> None:
    # Fingerprinting a teaser would make unrelated jobs look identical.
    assert fingerprint("Great opportunity!") is None
    assert fingerprint("x " * 200) is not None


def test_identical_text_fingerprints_identically_despite_formatting() -> None:
    body = "We are hiring a financial analyst. " * 20

    assert fingerprint(body) == fingerprint(body.replace(" ", "  ").upper())


# --- Titles -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_display"),
    [
        ("Financial Analyst (Req #48210)", "Financial Analyst"),
        ("Financial Analyst - Job ID: 12345", "Financial Analyst"),
        ("URGENT HIRING: Financial Analyst", "Financial Analyst"),
        ("Financial Analyst (m/w/d)", "Financial Analyst"),
        ("Financial Analyst #900123", "Financial Analyst"),
    ],
)
def test_formatting_noise_is_removed_from_the_display_title(
    raw: str, expected_display: str
) -> None:
    title = normalize_title(raw)

    assert title is not None
    assert title.display == expected_display


def test_the_original_wording_survives_normalisation() -> None:
    title = normalize_title("Senior FP&A Analyst, Corporate")

    assert title is not None
    # Display keeps the employer's phrasing; only the key is flattened.
    assert title.display == "Senior FP&A Analyst, Corporate"
    assert title.key == "senior fp a analyst corporate"


def test_a_title_of_only_punctuation_is_unusable() -> None:
    assert normalize_title("---") is None
    assert normalize_title("   ") is None
    assert normalize_title(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Financial Analyst", Seniority.SENIOR),
        ("Sr Financial Analyst", Seniority.SENIOR),
        ("Junior Financial Analyst", Seniority.JUNIOR),
        ("Jr. Financial Analyst", Seniority.JUNIOR),
        ("Financial Analyst Intern", Seniority.INTERN),
        ("Principal Financial Analyst", Seniority.PRINCIPAL),
        ("Finance Manager", Seniority.MANAGER),
        ("VP of Finance", Seniority.EXECUTIVE),
        ("Financial Analyst", None),
    ],
)
def test_seniority_is_read_from_the_title(raw: str, expected: Seniority | None) -> None:
    assert extract_seniority(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Financial Analyst II", 2),
        ("Financial Analyst III", 3),
        ("Financial Analyst 2", 2),
        ("Financial Analyst", None),
        # A lone "I" is far more likely to be a word than a level.
        ("Financial Analyst I", None),
    ],
)
def test_level_markers_are_read_only_from_the_end_of_a_title(
    raw: str, expected: int | None
) -> None:
    assert extract_level(raw) == expected


def test_seniority_abbreviations_are_expanded_for_comparison() -> None:
    equivalent, _, reason = title_equivalence(
        normalize_title("Sr. Financial Analyst"),  # type: ignore[arg-type]
        normalize_title("Senior Financial Analyst"),  # type: ignore[arg-type]
    )

    assert equivalent is True, reason


def test_work_arrangement_qualifiers_do_not_change_the_role() -> None:
    equivalent, _, _ = title_equivalence(
        normalize_title("Financial Analyst (Remote)"),  # type: ignore[arg-type]
        normalize_title("Financial Analyst - Full Time"),  # type: ignore[arg-type]
    )

    assert equivalent is True


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Senior Financial Analyst", "Junior Financial Analyst"),
        ("Financial Analyst II", "Financial Analyst III"),
        ("Staff Financial Analyst", "Senior Financial Analyst"),
        ("Financial Analyst", "Finance Manager"),
        ("Financial Analyst", "Data Analyst"),
    ],
)
def test_different_seniority_or_role_is_never_equivalent(
    left: str, right: str
) -> None:
    equivalent, _, reason = title_equivalence(
        normalize_title(left),  # type: ignore[arg-type]
        normalize_title(right),  # type: ignore[arg-type]
    )

    assert equivalent is False, f"{left!r} and {right!r} merged: {reason}"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remote", WorkMode.REMOTE),
        ("Work from home", WorkMode.REMOTE),
        ("Hybrid", WorkMode.HYBRID),
        # "Hybrid remote" is hybrid: the more specific reading wins.
        ("Hybrid remote", WorkMode.HYBRID),
        ("On-site", WorkMode.ONSITE),
        ("Boston, MA", None),
        (None, None),
    ],
)
def test_work_mode_is_read_never_guessed(text: str | None, expected) -> None:
    assert detect_work_mode(text) is expected


# --- Companies --------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Acme Ltd", "Acme Limited"),
        ("Acme, Inc.", "ACME"),
        ("The Home Depot", "Home Depot"),
        ("Acme Holdings Pvt Ltd", "Acme Holdings"),
        ("Northside Hospital", "Northside Hospital Atlanta"),
    ],
)
def test_the_same_employer_written_differently_matches(left: str, right: str) -> None:
    matched, reason = companies_match(normalize_company(left), normalize_company(right))

    assert matched is True, f"{left!r} and {right!r} did not match: {reason}"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Apollo Group", "Apollo Holdings"),
        # A one-word prefix is far too weak to merge on.
        ("Square", "Square Enix"),
        ("Bank of America", "Bank of the West"),
        ("Acme Technologies", "Acme Systems"),
        ("Leidos", "Lockheed Martin"),
    ],
)
def test_different_employers_are_never_merged(left: str, right: str) -> None:
    matched, reason = companies_match(normalize_company(left), normalize_company(right))

    assert matched is False, f"{left!r} and {right!r} merged: {reason}"


def test_a_missing_employer_is_not_a_match() -> None:
    # Without an employer there is no way to tell two "Financial Analyst"
    # postings apart.
    matched, reason = companies_match(None, normalize_company("Acme"))

    assert matched is False
    assert "missing" in reason


def test_the_legal_suffix_survives_in_the_display_name() -> None:
    company = normalize_company("Acme Ltd")

    assert company is not None
    assert company.display == "Acme Ltd"
    assert company.key == "acme"


def test_a_name_that_is_only_a_legal_form_still_compares() -> None:
    company = normalize_company("Limited")

    assert company is not None
    assert company.key == "limited"


# --- Locations --------------------------------------------------------------


def test_location_display_is_tidied_not_invented() -> None:
    location = normalize_location("  Boston , MA ")

    assert location.display == "Boston, MA"
    # No country, no coordinates, no expansion: only what the portal said.
    assert location.parts == ("boston", "ma")


def test_a_city_sharing_a_state_name_keeps_its_city() -> None:
    # "New York, NY" must not collapse to "ny ny", or every city in the state
    # becomes a less specific form of it.
    assert normalize_location("New York, NY").key == "new york ny"
    assert normalize_location("New York, New York").key == "new york ny"
    assert normalize_location("Buffalo, NY").key == "buffalo ny"


def test_state_spelling_does_not_change_the_place() -> None:
    assert normalize_location("Boston, Massachusetts").key == normalize_location(
        "Boston, MA"
    ).key


def test_remote_is_preserved_as_a_mode_not_a_place() -> None:
    location = normalize_location("Remote, United States")

    assert location.work_mode is WorkMode.REMOTE
    assert location.is_placeless
    assert location.display == "Remote, United States"


def test_work_mode_can_come_from_the_title_when_the_location_is_a_city() -> None:
    location = normalize_location(
        "Philadelphia, PA", extra_mode_text="Senior Financial Analyst - Remote"
    )

    assert location.work_mode is WorkMode.REMOTE
    assert location.parts == ("philadelphia", "pa")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Boston, MA", "Boston"),
        ("Boston, MA", "Boston, Massachusetts"),
        ("Remote", "New York, NY"),
        ("New York, NY", None),
    ],
)
def test_compatible_locations_do_not_block_a_merge(
    left: str, right: str | None
) -> None:
    compatible, reason = locations_compatible(
        normalize_location(left), normalize_location(right)
    )

    assert compatible is True, reason


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Boston, MA", "Denver, CO"),
        # Same state, different city: sharing a region is not enough.
        ("New York, NY", "Buffalo, NY"),
        ("London, UK", "Manchester, UK"),
    ],
)
def test_conflicting_locations_block_a_merge(left: str, right: str) -> None:
    compatible, reason = locations_compatible(
        normalize_location(left), normalize_location(right)
    )

    assert compatible is False, f"{left!r} and {right!r} judged compatible: {reason}"


def test_two_remote_postings_with_different_hubs_stay_compatible() -> None:
    compatible, _ = locations_compatible(
        normalize_location("Remote - New York, NY"),
        normalize_location("Remote - Austin, TX"),
    )

    assert compatible is True


# --- Employment type and dates ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Full-time", "Full-time"),
        ("FULL TIME", "Full-time"),
        ("fulltime", "Full-time"),
        ("Part time", "Part-time"),
        ("Contract", "Contract"),
        ("Internship", "Internship"),
        (None, None),
        ("   ", None),
    ],
)
def test_employment_types_are_folded_to_one_spelling(
    raw: str | None, expected: str | None
) -> None:
    assert normalize_employment_type(raw) == expected


def test_an_unrecognised_employment_type_is_kept_as_written() -> None:
    # The portal knows its own vocabulary; dropping the value loses real data.
    assert normalize_employment_type("Fractional CFO engagement") == (
        "Fractional CFO engagement"
    )


def test_naive_posting_dates_are_read_as_utc() -> None:
    result = normalize_posted_at(datetime(2026, 8, 3, 14, 48))

    assert result == datetime(2026, 8, 3, 14, 48, tzinfo=UTC)


def test_a_posting_date_far_in_the_future_is_dropped() -> None:
    # Broken data would otherwise sit at the top of "newest first" forever.
    now = datetime(2026, 8, 4, tzinfo=UTC)

    assert normalize_posted_at(now + timedelta(days=30), now=now) is None
    # Small clock skew between portals is tolerated.
    assert normalize_posted_at(now + timedelta(hours=6), now=now) is not None


def test_an_implausibly_old_posting_date_is_dropped() -> None:
    assert normalize_posted_at(EARLIEST_PLAUSIBLE_POSTING - timedelta(days=1)) is None
