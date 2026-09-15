"""The shipped SmartRecruiters board list, and how it earned its entries.

SmartRecruiters publishes no cross-company search and no employer directory, so
this portal's coverage *is* ``DEFAULT_BOARDS`` and nothing wider. That makes the
list a data asset rather than a constant, and these tests guard the properties
that keep it trustworthy:

* every slug was **verified live** - it returned a board page carrying
  SmartRecruiters' own ``companyIdentifier`` for that slug, on the permitted
  HTML path;
* no slug was **inferred from a company name**. Measured across 70 plausible
  candidates, 23 resolved - 33% - so a name is a hypothesis rather than a slug,
  and a sample of the ones that do not exist is pinned here as absent;
* a board that resolves is not automatically shipped. Six verified boards are
  deliberately absent, each with its reason recorded below - three because they
  cost far more than they return, two because they return nothing, and one
  because it belongs to an employer that opted out;
* **no employer on the vendor's own opt-out list may appear.** That list is the
  one thing here that is not a performance or coverage question: it is what an
  employer asked for, and it is enforced twice - once when the board list is
  built, and again before any request.

These tests are offline. They assert the shape and membership of the list, not
that the boards answer today - a network assertion would make the suite fail
whenever an employer paused hiring.
"""

from __future__ import annotations

import pytest

from app.portals.adapters.smartrecruiters import (
    DEFAULT_BOARDS,
    OPTED_OUT_BY_IDENTITY,
    OPTED_OUT_EMPLOYERS,
    SmartRecruitersPortal,
    is_opted_out,
)

#: The boards the portal shipped with, each confirmed to return its own
#: ``companyIdentifier`` on ``careers.smartrecruiters.com/<slug>``. Ordered by
#: measured fresh-US analyst volume, because the walk stops once the request's
#: limit is met and the richest boards should be reached first.
SHIPPED = (
    "WellmarkInc",
    "NBCUniversal3",
    "Keenfinity",
    "PublicStorage",
    "Versant3",
    "CapTechConsulting",
    "AbbVie",
    "WesternDigital",
    "Sandisk",
    "ServiceNow",
    "Experian",
    "AIRCommunities",
    "TheNielsenCompany",
    "CRB",
    "AcumenSolutions",
    "Equinox",
    "Sodexo",
)

#: Slugs that do **not** resolve. Every one is a real company that plausibly
#: uses SmartRecruiters, and every one failed to return a board carrying its own
#: identifier when probed. Listed so that adding one by inference fails loudly
#: here rather than quietly costing the portal a board at run time - and, worse
#: on this portal than most, a slug that does not exist answers HTTP 200.
DOES_NOT_RESOLVE = (
    "Visa",
    "Skechers",
    "LinkedIn",
    "Walmart",
    "Biogen",
    "IKEA",
    "McDonalds",
    "Square",
    "Atlassian",
    "DoorDash",
    "Instacart",
    "Peloton",
    "Chewy",
    "Etsy",
    "Zillow",
    "Deloitte",
    "Accenture",
    "IBM",
    "Oracle",
    "Adobe",
    "Intuit",
    "Nike",
    "Starbucks",
    "Target",
    "Medtronic",
    "Pfizer",
    "Equifax",
    "Nasdaq",
)

#: Boards that resolve and are deliberately absent, with the reason. Pinned so
#: that restoring one on the strength of "it exists and it is verified" has to
#: be a decision.
#:
#: The three staffing boards are the interesting case. They resolve, they are
#: real employers, and their fuzzy search answers almost any analyst query with
#: hundreds of near-duplicate listings - so every one of those titles costs a
#: job page, because a SmartRecruiters card publishes no date and the only way
#: to learn one is to open the posting. Measured over both test resumes they
#: were 204 of 273 job-page fetches, 75% of the portal's entire cost, and
#: produced no qualifying job for either resume.
RESOLVES_BUT_EXCLUDED = {
    "BoschGroup": (
        "on the vendor's opt-out list, and separately serves jobs.bosch.com "
        "rather than a SmartRecruiters board"
    ),
    "AveryDennison": "on the vendor's opt-out list",
    "Ubisoft": "on the vendor's opt-out list; the board is now Ubisoft2",
    "Ubisoft2": (
        "the same employer as the opted-out Ubisoft - see "
        "test_the_ubisoft_board_is_excluded_as_the_same_employer"
    ),
    "ClarkDavisAssociates": (
        "staffing board: 206 cards, 97 job-page fetches for the two test "
        "resumes, no qualifying job"
    ),
    "JobsForHumanity": (
        "staffing board: 850 cards, 65 job-page fetches, no qualifying job"
    ),
    "prosidianconsulting": (
        "staffing board: 863 cards, 42 job-page fetches, no qualifying job"
    ),
    "AuthenticBrandsGroup": "resolves with zero cards",
    "Wayfair": "resolves with zero cards",
}


def test_every_shipped_board_is_present() -> None:
    assert DEFAULT_BOARDS == SHIPPED


def test_no_unverified_slug_has_been_added_by_inference() -> None:
    """The failure mode this list invites: adding Visa because it exists."""
    assert set(DEFAULT_BOARDS).isdisjoint(DOES_NOT_RESOLVE)


def test_no_knowingly_excluded_board_has_crept_in() -> None:
    assert set(DEFAULT_BOARDS).isdisjoint(RESOLVES_BUT_EXCLUDED)


def test_no_shipped_board_is_on_the_vendors_opt_out_list() -> None:
    """The one rule here that is about somebody's wishes, not our coverage."""
    for slug in DEFAULT_BOARDS:
        assert not is_opted_out(slug), f"{slug} asked not to be crawled"


def test_the_ubisoft_board_is_excluded_as_the_same_employer() -> None:
    """``Ubisoft`` is closed, and ``Ubisoft2`` is Ubisoft.

    The published rule is ``Disallow: /Ubisoft/*``, a path prefix that does not
    literally match ``/Ubisoft2``. Taking that as permission would be the wrong
    reading, and the evidence says so plainly. Checked live:

    * the ``Ubisoft2`` board publishes ``og:site_name`` of "Ubisoft";
    * it titles itself "Careers at Ubisoft";
    * its job pages name the hiring organisation "Ubisoft";
    * the opted-out ``Ubisoft`` slug no longer resolves at all.

    So the employer moved boards and the rule did not follow. The exclusion is
    recorded in ``OPTED_OUT_BY_IDENTITY`` rather than bolted onto the vendor's
    own list, which stays byte-for-byte auditable against the published file.
    """
    assert "Ubisoft" in OPTED_OUT_EMPLOYERS
    # The vendor's list is left exactly as published.
    assert "Ubisoft2" not in OPTED_OUT_EMPLOYERS
    # Ours carries the identity decision.
    assert "Ubisoft2" in OPTED_OUT_BY_IDENTITY

    assert is_opted_out("Ubisoft") is True
    assert is_opted_out("Ubisoft2") is True
    assert "Ubisoft2" not in DEFAULT_BOARDS
    assert "Ubisoft" not in DEFAULT_BOARDS


def test_the_vendors_published_list_is_not_edited_to_add_our_decisions() -> None:
    """Provenance matters: one set is theirs, one is ours."""
    assert OPTED_OUT_EMPLOYERS.isdisjoint(OPTED_OUT_BY_IDENTITY)
    assert len(OPTED_OUT_EMPLOYERS) == 68


def test_an_identity_excluded_board_cannot_be_configured_back_in() -> None:
    portal = SmartRecruitersPortal({"boards": ["Ubisoft2", "WellmarkInc"]})

    assert portal.boards == ("WellmarkInc",)


def test_the_removed_staffing_boards_stay_removed() -> None:
    """They resolve and they are verified; they are still not worth a request."""
    for slug in ("ClarkDavisAssociates", "JobsForHumanity", "prosidianconsulting"):
        assert slug in RESOLVES_BUT_EXCLUDED
        assert slug not in DEFAULT_BOARDS
        # Not an opt-out question - these employers never asked for anything.
        assert is_opted_out(slug) is False


def test_an_opted_out_employer_cannot_be_configured_back_in() -> None:
    """Configuration is not a way round the list."""
    portal = SmartRecruitersPortal({"boards": ["AveryDennison", "BoschGroup"]})

    assert portal.boards == ()


def test_slugs_are_unique() -> None:
    assert len(DEFAULT_BOARDS) == len(set(DEFAULT_BOARDS))


def test_slugs_are_plain_url_path_segments() -> None:
    for slug in DEFAULT_BOARDS:
        assert slug == slug.strip()
        assert "/" not in slug
        assert "?" not in slug
        assert "&" not in slug
        assert " " not in slug


def test_no_employer_name_is_configured_anywhere() -> None:
    """The whole list is slugs, because the job page supplies the employer.

    If an entry were ever a mapping carrying a ``company``, this portal would
    have quietly acquired the stale-name problem ``workday.py`` has to live
    with.
    """
    for entry in DEFAULT_BOARDS:
        assert isinstance(entry, str)


def test_the_default_list_is_what_an_unconfigured_portal_reads() -> None:
    assert SmartRecruitersPortal().boards == DEFAULT_BOARDS


def test_configuration_replaces_the_defaults_entirely() -> None:
    """Adding an employer must never require a deployment."""
    assert SmartRecruitersPortal({"boards": ["Acme"]}).boards == ("Acme",)


@pytest.mark.parametrize("slug", DEFAULT_BOARDS)
def test_each_board_builds_only_permitted_urls(slug: str) -> None:
    portal = SmartRecruitersPortal()

    board = portal.board_url(slug)
    search = portal.search_url(slug, "financial analyst")

    for url in (board, search):
        assert url.startswith("https://careers.smartrecruiters.com/")
        assert "api.smartrecruiters.com" not in url
    assert search.startswith(f"{board}?search=")


def test_the_shipped_count_is_what_was_actually_verified() -> None:
    """Twenty-three resolved; six are deliberately not shipped."""
    assert len(DEFAULT_BOARDS) == 17
