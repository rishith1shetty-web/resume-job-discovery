"""The shipped Lever board list, and how it earned its entries.

Lever publishes no cross-company search, so this portal's coverage *is*
``DEFAULT_BOARDS`` and nothing wider. That makes the list a data asset rather
than a constant, and these tests guard the three properties that keep it
trustworthy:

* every slug was **verified live** - it resolves to a JSON array of postings;
* every employer name was **read, not derived**. The feed carries no company
  name at all, so the name came from that board's own
  ``schema.org/JobPosting`` ``hiringOrganization.name`` on a real job page;
* no slug was **inferred from a company name**. Measured across 88 plausible
  candidates, 11 resolved - 12.5% - so a name is a hypothesis rather than a
  slug, and the ones that 404 are pinned here as absent.

These tests are offline. They assert the shape and membership of the list, not
that the boards answer today - a network assertion would make the suite fail
whenever an employer paused hiring.
"""

from __future__ import annotations

import pytest

from app.portals.adapters.lever import DEFAULT_BOARDS, LeverPortal

#: The boards the portal shipped with. Each resolved against
#: ``/v0/postings/<slug>?mode=json`` with HTTP 200 and a JSON array, and each
#: carried United States postings at the time it was added.
SHIPPED = {
    "gopuff": "Gopuff",
    "palantir": "Palantir Technologies",
    "spotify": "Spotify",
    "matchgroup": "Match Group",
    "binance": "Binance",
    "fly": "Fly.io",
}

#: Boards that answer 200 and are deliberately **not** shipped.
#:
#: ``cred`` publishes 11 postings, none of them in the United States. The other
#: three are currently empty - and an empty board serves no job page, so its
#: employer name could only have been guessed. Listed here so that adding one
#: on the strength of "it exists" fails loudly rather than quietly shipping a
#: board that costs a request and returns nothing.
RESOLVES_BUT_EXCLUDED = ("cred", "kraken", "wealthsimple", "brightwheel")

#: Slugs that do **not** exist. Every one is a real company that plausibly uses
#: Lever, and every one returned HTTP 404 from the postings feed when probed.
#: They are listed so that adding one by inference fails loudly here rather
#: than quietly costing the portal a board at runtime.
DOES_NOT_RESOLVE = (
    "netflix",
    "figma",
    "notion",
    "rippling",
    "reddit",
    "stripe",
    "deel",
    "airtable",
    "duolingo",
    "grammarly",
    "vercel",
    "miro",
)


def test_every_shipped_board_is_present_with_its_employer() -> None:
    assert dict(DEFAULT_BOARDS) == SHIPPED


def test_no_board_resolves_but_excluded_slug_has_crept_in() -> None:
    shipped = {slug for slug, _ in DEFAULT_BOARDS}

    assert shipped.isdisjoint(RESOLVES_BUT_EXCLUDED)


def test_no_unverified_slug_has_been_added_by_inference() -> None:
    """The failure mode this list invites: adding Netflix because it exists."""
    shipped = {slug for slug, _ in DEFAULT_BOARDS}

    assert shipped.isdisjoint(DOES_NOT_RESOLVE)


def test_every_board_names_an_employer() -> None:
    """A board with no company name would publish listings from nobody.

    The feed carries no employer name, so an unnamed board means every one of
    its listings reaches the dashboard with a blank company - which also breaks
    canonical grouping, because ``build_canonical_key`` falls back to the URL
    when there is no employer to group on.
    """
    for slug, company in DEFAULT_BOARDS:
        assert company, f"{slug} must name its employer"
        assert company == company.strip()


def test_slugs_are_unique() -> None:
    slugs = [slug for slug, _ in DEFAULT_BOARDS]

    assert len(slugs) == len(set(slugs))


def test_slugs_are_url_safe_path_segments() -> None:
    for slug, _ in DEFAULT_BOARDS:
        assert slug == slug.strip()
        assert "/" not in slug
        assert "?" not in slug
        assert " " not in slug


def test_the_default_list_is_what_an_unconfigured_portal_reads() -> None:
    assert [board.as_tuple() for board in LeverPortal().boards] == list(DEFAULT_BOARDS)


def test_configuration_replaces_the_defaults_entirely() -> None:
    """Adding an employer must never require a deployment."""
    adapter = LeverPortal({"boards": [{"slug": "acme", "company": "Acme Corp"}]})

    assert [board.as_tuple() for board in adapter.boards] == [("acme", "Acme Corp")]


@pytest.mark.parametrize("slug", [slug for slug, _ in DEFAULT_BOARDS])
def test_each_board_builds_the_public_feed_url(slug: str) -> None:
    board = next(b for b in LeverPortal().boards if b.slug == slug)

    assert board.feed_url() == f"https://api.lever.co/v0/postings/{slug}"
    # The authenticated API is a different host path and is never constructed.
    assert "/v1/" not in board.feed_url()
