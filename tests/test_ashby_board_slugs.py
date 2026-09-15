"""The shipped Ashby board list, and how it earned its entries.

Ashby publishes no cross-company search and no board directory, so this
portal's coverage *is* ``DEFAULT_BOARDS`` and nothing wider. That makes the
list a data asset rather than a constant, and these tests guard the properties
that keep it trustworthy:

* every slug was **verified live** - it returned a board page carrying an
  organisation name and a posting collection, on the robots-permitted path;
* no slug was **inferred from a company name**. Measured across 169 plausible
  candidates, 61 resolved - 36% - so a name is a hypothesis rather than a slug,
  and a sample of the ones that do not exist is pinned here as absent;
* **no employer names are configured at all.** Ashby publishes the organisation
  on the board page, so a board is addressed by slug alone and cannot carry a
  stale or invented company name. That is the difference from ``lever.py`` and
  ``dayforce.py``, and it is asserted rather than left to review.

These tests are offline. They assert the shape and membership of the list, not
that the boards answer today - a network assertion would make the suite fail
whenever an employer paused hiring.
"""

from __future__ import annotations

import pytest

from app.portals.adapters.ashby import BOARD_BASE, DEFAULT_BOARDS, AshbyPortal

#: The boards the portal shipped with, each confirmed to return an
#: organisation and a posting list on ``jobs.ashbyhq.com/<slug>``. The names in
#: the comments are what the page itself reported - they are recorded here for
#: the reader, and deliberately *not* used by the adapter, which reads them
#: from the page at run time.
SHIPPED = (
    "openai",  # OpenAI
    "snowflake",  # Snowflake
    "plaid",  # Plaid
    "perplexity",  # Perplexity
    "ramp",  # Ramp
    "cohere",  # Cohere
    "elevenlabs",  # ElevenLabs
    "whoop",  # Whoop
    "vanta",  # Vanta
    "handshake",  # Handshake
    "cerebras",  # Cerebras Systems
    "replit",  # Replit
    "benchling",  # Benchling
    "notion",  # Notion
    "temporal",  # Temporal Technologies
    "poshmark",  # Poshmark
    "clickup",  # ClickUp
    "sentry",  # Sentry
    "strava",  # Strava
    "watershed",  # Watershed
    "prefect",  # Prefect
    "render",  # Render
    "airbyte",  # Airbyte
    "hex",  # Hex
    "circle",  # Circle
    "sardine",  # Sardine
    "astronomer",  # Astronomer
    "alchemy",  # Alchemy
)

#: Slugs that do **not** exist. Every one is a real company that plausibly uses
#: Ashby, and every one failed to return a board when probed. Listed so that
#: adding one by inference fails loudly here rather than quietly costing the
#: portal a board at run time - and, worse on this portal than most, a slug
#: that does not exist answers HTTP 200.
DOES_NOT_RESOLVE = (
    "anthropic",
    "figma",
    "retool",
    "brex",
    "scale",
    "rippling",
    "gusto",
    "amplitude",
    "mixpanel",
    "segment",
    "netlify",
    "sourcegraph",
    "huggingface",
    "mistral",
    "chainalysis",
    "calm",
    "clearbit",
    "gong",
    "wellfound",
)

#: Boards that resolve but are deliberately absent, with the reason. Pinned so
#: that adding one on the strength of "it exists" has to be a decision.
RESOLVES_BUT_EXCLUDED = {
    "deel": "resolves with zero postings",
    "airtable": "resolves with zero postings",
    "mercury": "resolves with zero postings",
    "loom": "resolves with zero postings",
    "vercel": "resolves with zero postings",
    "wiz": "resolves with zero postings",
    "miro": "postings carry no United States locations",
    "clickhouse": "postings carry no United States locations",
}


def test_every_shipped_board_is_present() -> None:
    assert DEFAULT_BOARDS == SHIPPED


def test_no_unverified_slug_has_been_added_by_inference() -> None:
    """The failure mode this list invites: adding Anthropic because it exists."""
    assert set(DEFAULT_BOARDS).isdisjoint(DOES_NOT_RESOLVE)


def test_no_knowingly_excluded_board_has_crept_in() -> None:
    assert set(DEFAULT_BOARDS).isdisjoint(RESOLVES_BUT_EXCLUDED)


def test_slugs_are_unique() -> None:
    assert len(DEFAULT_BOARDS) == len(set(DEFAULT_BOARDS))


def test_slugs_are_plain_url_path_segments() -> None:
    for slug in DEFAULT_BOARDS:
        assert slug == slug.strip()
        assert slug == slug.casefold()
        assert "/" not in slug
        assert "?" not in slug
        assert " " not in slug


def test_no_employer_name_is_configured_anywhere() -> None:
    """The whole list is slugs, because the page supplies the employer.

    If an entry were ever a mapping carrying a ``company``, this portal would
    have quietly acquired the stale-name problem the other per-employer
    adapters have to live with.
    """
    for entry in DEFAULT_BOARDS:
        assert isinstance(entry, str)


def test_the_default_list_is_what_an_unconfigured_portal_reads() -> None:
    assert AshbyPortal().boards == DEFAULT_BOARDS


def test_configuration_replaces_the_defaults_entirely() -> None:
    """Adding an employer must never require a deployment."""
    adapter = AshbyPortal({"boards": ["acme"]})

    assert adapter.boards == ("acme",)


@pytest.mark.parametrize("slug", DEFAULT_BOARDS)
def test_each_board_builds_only_permitted_urls(slug: str) -> None:
    adapter = AshbyPortal()

    board = adapter.board_url(slug)
    job = adapter.job_url(slug, "00000000-0000-4000-8000-000000000000")
    apply_to = adapter.apply_url(slug, "00000000-0000-4000-8000-000000000000")

    for url in (board, job, apply_to):
        assert url.startswith(f"{BOARD_BASE}/")
        tail = url[len(BOARD_BASE) :]
        # The three prefixes this host's robots.txt closes.
        assert not tail.startswith("/api/")
        assert not tail.startswith("/b/")
        assert not tail.startswith("/meeting/")
    assert apply_to == f"{job}/application"
