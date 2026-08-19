"""The shipped Greenhouse board list, and how it earned its entries.

Greenhouse publishes no cross-company search, so this portal's coverage *is*
``DEFAULT_BOARD_TOKENS`` and nothing wider. That makes the list a data asset
rather than a constant, and these tests guard the two properties that keep it
trustworthy:

* every token was **verified live** - it resolves and reports the employer it
  is supposed to belong to;
* no token was **inferred from a company name**. Thirty plausible-looking
  candidates return 404 and are pinned here as absent, because the failure mode
  this list invites is someone adding ``wayfair`` because Wayfair exists.

These tests are offline. They assert the shape and membership of the list, not
that the boards answer today - a network assertion would make the suite fail
whenever an employer paused hiring.
"""

from __future__ import annotations

import pytest

from app.portals.adapters.greenhouse import DEFAULT_BOARD_TOKENS, GreenhousePortal

#: The eight the portal shipped with. None may be dropped: they are still
#: verified, and removing an employer silently removes its coverage.
ORIGINAL = (
    "stripe",
    "databricks",
    "brex",
    "affirm",
    "instacart",
    "chime",
    "flexport",
    "robinhood",
)

#: Added after live verification. Each resolved against the public Job Board
#: API and reported the expected employer in its own ``company_name``.
VERIFIED_ADDITIONS = (
    "esri",
    "checkr",
    "enova",
    "mark43",
    "melio",
    "lyft",
    "samsara",
    "anthropic",
    "sofi",
    "peloton",
    "coinbase",
    "block",
    "fanduel",
    "oscar",
)

#: Tokens that do **not** exist. Every one is a real company that plausibly
#: uses Greenhouse, and every one returned HTTP 404 from the boards API when
#: probed. They are listed so that adding one by inference fails loudly here
#: rather than quietly costing a portal its whole leg at runtime.
KNOWN_INVALID = (
    "doordash",
    "plaid",
    "wealthfront",
    "ramp",
    "addepar",
    "devoted",
    "cityblock",
    "includedhealth",
    "hims",
    "ro",
    "warbyparker",
    "wayfair",
    "chewy",
    "thredup",
    "nerdwallet",
    "creditkarma",
    "square",
    "kraken",
    "circle",
    "chainalysis",
    "dave",
    "stash",
    "acorns",
    "draftkings",
    "etsy",
    "opendoor",
    "compass",
    "zillow",
    "redfin",
    "lendingclub",
)

#: Boards that resolve but must not be shipped. ``marqeta`` and
#: ``patientpointinviteonly`` returned zero postings; the Coupang and
#: PatientPoint boards are internal or invite-only rather than public; the
#: Dayforce namespaces are a different platform entirely.
EXCLUDED_ON_PURPOSE = (
    "marqeta",
    "patientpointinviteonly",
    "coupanginternal",
    "lifelabs",
    "wechu",
    "afsc",
)


@pytest.mark.parametrize("token", ORIGINAL)
def test_the_original_boards_are_still_configured(token: str) -> None:
    assert token in DEFAULT_BOARD_TOKENS


@pytest.mark.parametrize("token", VERIFIED_ADDITIONS)
def test_each_verified_addition_is_configured(token: str) -> None:
    assert token in DEFAULT_BOARD_TOKENS


@pytest.mark.parametrize("token", KNOWN_INVALID)
def test_no_unverified_token_is_configured(token: str) -> None:
    """A 404 board costs a request and returns nothing, every single run."""
    assert token not in DEFAULT_BOARD_TOKENS


@pytest.mark.parametrize("token", EXCLUDED_ON_PURPOSE)
def test_boards_excluded_on_purpose_stay_excluded(token: str) -> None:
    assert token not in DEFAULT_BOARD_TOKENS


def test_the_list_is_exactly_the_verified_set() -> None:
    assert set(DEFAULT_BOARD_TOKENS) == set(ORIGINAL) | set(VERIFIED_ADDITIONS)


def test_no_token_is_listed_twice() -> None:
    """A duplicate is a wasted request on every run, not a doubled result."""
    assert len(DEFAULT_BOARD_TOKENS) == len(set(DEFAULT_BOARD_TOKENS))


def test_tokens_are_url_safe_path_segments() -> None:
    """Each token goes straight into the request path."""
    for token in DEFAULT_BOARD_TOKENS:
        assert token == token.strip()
        assert token.islower()
        assert "/" not in token
        assert "?" not in token
        assert " " not in token


def test_the_defaults_are_what_an_unconfigured_portal_uses() -> None:
    assert GreenhousePortal()._boards == DEFAULT_BOARD_TOKENS


def test_an_operator_can_still_override_the_whole_list() -> None:
    """Coverage stays configuration: adding an employer needs no deployment."""
    adapter = GreenhousePortal({"board_tokens": ["acme"]})

    assert adapter._boards == ("acme",)
