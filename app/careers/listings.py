"""Reading the openings a browser-rendered job board would have shown.

# The problem this solves

Several applicant-tracking products serve an empty HTML shell and build the
job list in the browser. Measured on real employer boards, the tenant page
returns nothing a reader can use::

    avav.wd1.myworkdayjobs.com/avav        -> 200, 0 characters of text
    myjobs.adp.com/<employer>/cx           -> 200, 0 characters of text
    <tenant>.fa.<region>.oraclecloud.com/… -> 200, 16 characters

Those are real, public, employer-specific boards. Judged on their HTML alone
they have no job evidence, so the honest verdict is "unverified" and the
employer is dropped - which is correct but costs a lot of good results, since
Workday alone accounts for roughly a third of the employer application links
this project collects.

# What is done instead, and what is not

The board's own page fetches its openings from a public endpoint. This asks
that same endpoint the same question - no authentication, no key, no private
API, the honest ``JobFinderBot`` user agent, and ``robots.txt`` consulted like
any other request (Workday's, checked live, allows it and disallows only
``/refreshFacet/``).

What comes back is used as evidence that the board is real and currently has
openings. It is *not* turned into job results: nothing here produces a
``RawJob``, nothing is scored, and no opening reaches the Find 20/50/100
allocation. A career page is still a company and a page.

# Why this is not a company list

The tenant and the board name are read out of the URL the employer's own
posting supplied. No company name appears in this module, and no domain is
constructed from one. What is encoded is a vendor's URL grammar - the same kind
of knowledge ``app.processing.urls`` already keeps in ``_ATS_HOSTS`` - so an
employer is reached because their posting pointed at their board, never
because they were on a list.

Adding a vendor is adding one function to ``_PROBES``. A vendor with no public
listing endpoint simply has none, and its employers are judged on their HTML
like everyone else.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.careers.fetching import CareerPageFetcher
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Openings read from one probe. Enough to prove the board is live and to say
#: how many were seen; this is evidence, not a result set.
PROBE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class ListingProbe:
    """What a board's public listing endpoint reported."""

    #: Openings the endpoint says exist in total, when it reports a total.
    total: int
    #: Titles actually returned, for the reason text and the role check.
    titles: tuple[str, ...]

    @property
    def has_openings(self) -> bool:
        return self.total > 0 or bool(self.titles)


_WORKDAY = re.compile(
    r"^https?://(?P<tenant>[^./]+)\.(?P<wd>wd\d+)\."
    r"(?P<domain>myworkdayjobs|myworkdaysite)\.com/"
    r"(?:(?P<locale>[a-z]{2}(?:[-_][A-Za-z]{2})?)/)?"
    r"(?P<site>[^/?#]+)",
    re.I,
)

#: Workday's locale segments are two-letter codes; a board named ``en`` would
#: be indistinguishable, so a single-segment path is treated as the board.
_LOCALE_ONLY = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")


async def _probe_workday(
    fetcher: CareerPageFetcher, url: str
) -> ListingProbe | None:
    """Ask a Workday board for its current openings.

    ``<tenant>.wdN.myworkdayjobs.com/<site>`` exposes its listings at
    ``/wday/cxs/<tenant>/<site>/jobs``, which is the endpoint the board's own
    page calls to draw itself.
    """
    match = _WORKDAY.match(url)
    if match is None:
        return None

    tenant = match.group("tenant")
    site = match.group("site")
    if not site or _LOCALE_ONLY.match(site):
        # The path held only a locale, so the board was not named.
        return None

    endpoint = (
        f"https://{tenant}.{match.group('wd')}.{match.group('domain')}.com"
        f"/wday/cxs/{tenant}/{site}/jobs"
    )
    payload = await fetcher.post_json(
        endpoint, {"limit": PROBE_LIMIT, "offset": 0, "searchText": ""}
    )
    if payload is None:
        return None

    postings = payload.get("jobPostings")
    if not isinstance(postings, list):
        return None

    titles = tuple(
        str(posting["title"])
        for posting in postings
        if isinstance(posting, dict) and isinstance(posting.get("title"), str)
    )
    total = payload.get("total")
    return ListingProbe(
        total=total if isinstance(total, int) and total >= 0 else len(titles),
        titles=titles,
    )


#: Vendor probes, tried in order. Each returns None when the URL is not its
#: shape, so adding one cannot disturb another.
_PROBES: tuple[Callable[[CareerPageFetcher, str], Awaitable[ListingProbe | None]], ...] = (
    _probe_workday,
)


async def probe_listings(
    fetcher: CareerPageFetcher, url: str
) -> ListingProbe | None:
    """Read a board's public openings, when its vendor publishes them.

    Returns None when no probe recognises the URL, when the endpoint declines,
    or when the response is not the shape it should be. None means "no
    evidence gathered" - never "no jobs" - and the caller treats it as such.
    """
    if not urlsplit(url).hostname:
        return None

    for probe in _PROBES:
        try:
            result = await probe(fetcher, url)
        except Exception as exc:  # noqa: BLE001 - a probe must never break a run
            logger.debug(
                "career_listing_probe_failed", url=url, error=type(exc).__name__
            )
            continue
        if result is not None:
            return result

    return None


__all__ = ["PROBE_LIMIT", "ListingProbe", "probe_listings"]
