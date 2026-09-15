"""SmartRecruiters adapter (``careers.smartrecruiters.com`` / ``jobs.smartrecruiters.com``).

Access surface, and why *this* one
----------------------------------
SmartRecruiters publishes each employer's board twice over, and this adapter
deliberately reads the slower of the two.

``api.smartrecruiters.com/robots.txt`` is explicit, and readable::

    User-agent: LinkedInBot
    Allow: /v1/companies/

    User-agent: *
    Disallow: /

The documented Posting API would return a whole board - descriptions, dates,
structured countries - in one request. It is **never contacted**. This is a
firmer refusal than the one ``ashby.py`` reasons about: there the policy could
not be read at all and declining was this project's own convention; here the
host publishes a global ``Disallow`` for every agent but one, and names that
one. There is nothing to weigh.

What is used instead is the public, server-rendered HTML path::

    https://careers.smartrecruiters.com/<Company>?search=<term>
    https://jobs.smartrecruiters.com/<Company>/<id>-<slug>

``jobs.smartrecruiters.com`` publishes no ``robots.txt`` at all (404, which
this project reads as "no restrictions published"), and
``careers.smartrecruiters.com`` answers its ``robots.txt`` with the
application shell rather than a rules file, which parses to no rules. Neither
host closes the paths used here.

The employer opt-out list
-------------------------
``www.smartrecruiters.com/robots.txt`` carries something unusual: a list of
**named employers** the vendor closes to crawlers - 68 of them,
``Disallow: /AveryDennison/*`` and so on. Those rules are scoped to ``www``,
and this adapter never requests ``www``; strictly, they do not bind it.

They are enforced anyway, as a hard exclusion checked before any request is
built. An employer that asked not to be crawled on the vendor's own domain has
said what it wants, and reaching the same postings through a sister hostname
would be honouring the letter of a rule while ignoring its point.

One case is worth stating plainly, because the naive reading goes the other
way. The list closes ``Ubisoft``; the board that exists today is ``Ubisoft2``,
which that path prefix does not match. It is excluded anyway. Checked live, the
``Ubisoft2`` board publishes ``og:site_name`` "Ubisoft", titles itself "Careers
at Ubisoft" and names the employer "Ubisoft" on its job pages, while the
opted-out ``Ubisoft`` slug no longer resolves at all: the employer moved
boards and the rule did not follow. ``OPTED_OUT_BY_IDENTITY`` carries that
decision, kept separate from the vendor's own list so the latter stays
auditable, and ``tests/test_smartrecruiters_boards.py`` records the evidence.

An ATS, not a job board
-----------------------
The same structural fact that shapes ``greenhouse.py``, ``lever.py``,
``ashby.py`` and ``workday.py``: SmartRecruiters publishes **no cross-company
search**. Coverage is exactly the configured boards.

Nothing may be trusted on its status code. A slug that does not exist answers
**200** - redirected to the product's own root - and an employer that has moved
its careers page away answers **200** with somebody else's website
(``BoschGroup`` serves ``jobs.bosch.com``). Both are soft failures, and
following redirects means the response body is the only evidence available. So
a board counts as real only when its page carries SmartRecruiters' own
``CAREERPAGE.companyIdentifier`` **and that identifier is the slug that was
asked for**. A valid board with no matching openings carries the identifier and
no cards, which is how "nothing today" stays distinguishable from "no such
employer".

Search, not enumeration
-----------------------
``?search=`` runs server-side across the whole board, and it reaches postings
that walking ``?page=`` does not: measured on one employer, paging reached 11
listings while a single search returned 35, nine of them unreachable by paging.
So search is the discovery mechanism and pagination is a bounded safety net,
not the normal path.

The search is fuzzy and relevance-ranked - it answers "data analyst" with "Data
Wrangler" - which is fine and expected. The same-role title gate re-filters
locally, and every title it rejects is a job page never requested.

Two hosts, and what that costs
------------------------------
Unlike ``workday.py``, where each employer is its own host, every board here
lives on one hostname and every job page on a second. So the configured rate
limit genuinely applies across all employers, and the board walk is
irreducibly sequential: this portal cannot be made faster by fanning out.

What it *can* do is use both hosts at once. The board walk and the job-page
fetches run against separate clients, so job pages for boards already read are
being fetched while the next board is still being searched. Everything is
cached for the life of the run - the search page per (board, query) and the job
page per URL - so a posting that answers three same-role variations costs one
fetch, which ``tests/test_smartrecruiters_adapter.py`` pins with a request
count.

Dates
-----
``itemprop="datePosted"`` is the original publication date. The page also emits
``og:updated_time`` carrying the same value; it is never read independently,
because a field that can diverge is exactly the trap that would let a refreshed
listing look new. Verified across 30 postings from 5 employers: ordered by
SmartRecruiters' own globally-sequential job id, ``datePosted`` was monotonic
with zero inversions, which a refresh timestamp could not be.

A missing or unparseable date leaves ``posted_at`` unset, so the freshness gate
rejects the listing rather than assuming it is recent.

This adapter collects and normalises. It does not score, deduplicate, rank or
apply to anything.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import quote

from app.core.constants import ApplicationType
from app.core.logging import get_logger
from app.portals.base import (
    BasePortal,
    PortalCapabilities,
    PortalHealth,
    PortalSearchRequest,
    PortalSearchResult,
    RawJob,
    declare_application_type,
)
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
)
from app.portals.http import HttpPolicy, PortalHttpClient

logger = get_logger(__name__)

#: The board and search host.
CAREERS_BASE = "https://careers.smartrecruiters.com"

#: The job-page and apply host.
JOBS_BASE = "https://jobs.smartrecruiters.com"

#: The host holding the one-request Posting API. Named only so the tests can
#: assert that nothing ever requests it - see the module docstring for why.
FORBIDDEN_API_HOST = "api.smartrecruiters.com"

#: Search result pages walked per (board, query). The search returns its whole
#: result set on one page - measured at 166 cards - so this is a safety net
#: against an employer large enough to change that, not the normal path.
MAX_SEARCH_PAGES = 3

#: Cards on a first page past which a second page is worth asking for. Set
#: above anything observed live, so the common case costs exactly one request.
PAGINATION_THRESHOLD = 150

#: Job pages in flight on the jobs host. **One**, deliberately.
#:
#: Every job page comes from a single hostname sharing a single rate limiter,
#: and ``_RateLimiter`` spaces *sequential* calls: several coroutines awaiting
#: it together all read the same last-request time, sleep the same interval and
#: then fire at once. Measured at eight, that issued 57 requests in 69 seconds -
#: about 49 a minute against a configured 30 - so the concurrency was not
#: buying speed honestly, it was quietly outrunning the pacing it appeared to
#: respect.
#:
#: Job pages still overlap the board walk, because that runs on the *other*
#: host with its own limiter. That is where this portal's real parallelism
#: lives, and it is genuine.
DEFAULT_MAX_CONCURRENT_JOBS = 1

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[a-z0-9]+")
_BLOCK_RE = re.compile(
    r"</(p|div|li|ul|ol|h[1-6]|tr|table|section|br)\s*>|<br\s*/?>", re.IGNORECASE
)
_LIST_ITEM_RE = re.compile(r"<li[^>]*>", re.IGNORECASE)

#: SmartRecruiters' own page config, which is what proves a board is real.
_COMPANY_ID_RE = re.compile(r'"companyIdentifier"\s*:\s*"([^"]*)"')

#: One search result card: the job link and its title.
_CARD_RE = re.compile(
    r'<a\s+href="(?P<url>https://jobs\.smartrecruiters\.com/[^"]+)"'
    r'[^>]*class="[^"]*js-job-ad-link[^"]*"[^>]*>\s*'
    r"<h\d[^>]*>(?P<title>.*?)</h\d>",
    re.IGNORECASE | re.DOTALL,
)

#: ``/<Company>/<numeric id>-<slug>``. The id is the stable external identity.
_JOB_ID_RE = re.compile(r"/([^/]+)/(\d{6,})(?:-|$)")

#: The published apply destination. Never constructed - see ``apply_url``.
_APPLY_RE = re.compile(
    r'href="(https://jobs\.smartrecruiters\.com/external-referrals/[^"]+)"',
    re.IGNORECASE,
)

#: The sectioned body of a job page: Company Description, Job Description,
#: Qualifications, Additional Information.
_SECTIONS_RE = re.compile(
    r'<div[^>]*class="[^"]*job-sections[^"]*"[^>]*>(.*?)</div>\s*</article>',
    re.IGNORECASE | re.DOTALL,
)
_SECTION_RE = re.compile(
    r'<section[^>]*class="[^"]*job-section[^"]*"[^>]*>(.*?)</section>',
    re.IGNORECASE | re.DOTALL,
)

#: The detail chips under the title: employment type, department, work
#: environment, pay grade. Only the first carries an ``itemprop``.
_JOB_DETAIL_RE = re.compile(
    r'<li[^>]*class="[^"]*job-detail[^"]*"[^>]*>(.*?)</li>',
    re.IGNORECASE | re.DOTALL,
)
_EMPLOYMENT_TYPE_RE = re.compile(
    r'<li[^>]*itemprop="employmentType"[^>]*>(.*?)</li>', re.IGNORECASE | re.DOTALL
)

#: Single letters match nearly every title and carry no signal: "FP&A Analyst"
#: must require "fp" and "analyst", not "a".
_MIN_TOKEN_LENGTH = 2

#: How SmartRecruiters spells the United States in ``addressCountry``.
US_COUNTRY_NAMES = frozenset(
    {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
)

#: SmartRecruiters' employment-type vocabulary, spelled the dashboard's way.
_EMPLOYMENT_TYPES = {
    "full-time": "Full-time",
    "full time": "Full-time",
    "part-time": "Part-time",
    "part time": "Part-time",
    "intern": "Intern",
    "internship": "Intern",
    "contract": "Contract",
    "contractor": "Contract",
    "temporary": "Temporary",
}

#: Employers SmartRecruiters' own ``www`` robots.txt closes to crawlers, by
#: name. Enforced as a hard exclusion before any request is built, even though
#: those rules are scoped to a host this adapter never touches - see the module
#: docstring.
#:
#: Captured from the live file, which carries 71 slug-shaped ``Disallow``
#: entries. Three of them - ``a``, ``referrals-portal`` and ``web-sso`` - are
#: the product's own paths rather than employers, so 68 remain. Listing those
#: three would cost nothing, but it would misrepresent what the list is.
OPTED_OUT_EMPLOYERS: frozenset[str] = frozenset(
    {
        "85DegreesBakeryCafe",
        "AccordServices",
        "AdeebaEServicesPvtLt",
        "AlcoaInc",
        "AmericanWell",
        "ArgusEventStaffing1",
        "AveryDennison",
        "AxiusTechnologiesInc",
        "BenCo",
        "BoschGroup",
        "CASES",
        "CROSSMARK1",
        "CircleOfCare",
        "CityofPhiladelphia",
        "ComputerConsultantsInternational1",
        "DiverseLynxLLC1",
        "EDMWorldMagazine",
        "EONRealityInc",
        "EndeavorITSolution",
        "Expeditors",
        "FarmersInsuranceDistrictCareerCe",
        "Firstsource3",
        "FriendshipVillageOfBloomington",
        "GARDACanadaSecurityCorporation",
        "GECPackagingTechnologiesLLC",
        "GoodwillSacramento",
        "GuardantHealth",
        "HookLogicInc",
        "IndigoBooksMusic",
        "Insitu1",
        "IntegratedResourcesINC",
        "JetSuite",
        "KMSTechnology1",
        "KSMConsulting",
        "LeoVegascom",
        "LifeCareServices",
        "MicroStrategy1",
        "MinnewaskaCommunityHealthService",
        "ModaOperandi",
        "MountTalentConsulting2",
        "NestlePurinaPetCare",
        "NewLifeOfCommunityHealthServices",
        "PTFrinaLestariNusantara",
        "PerkinsOilCompany",
        "PharmaCannis",
        "PhoenixHomeCare1",
        "PhoenixSeminary",
        "Pluralsight",
        "PurpleLandManagement",
        "QuotientLimited",
        "SNV",
        "STJOSEPHCENTE",
        "SchoolApply",
        "SolucionLLC1",
        "SproutsFarmersMarket",
        "StarSourceConsulting",
        "TheMillardGroup",
        "Tipico",
        "Ubisoft",
        "VisionElevatorsPtyLtd",
        "WBAYTV",
        "babich",
        "control4",
        "entirehire",
        "flo-cert",
        "mynetworksolutions",
        "skyward1",
        "syntelinc1",
    }
)

#: Boards that are **not** on the vendor's list but belong to an employer that
#: is. SmartRecruiters writes its rules as path prefixes, so
#: ``Disallow: /Ubisoft/*`` does not match ``/Ubisoft2``. That is a fact about
#: prefix matching, not a statement about what the employer asked for.
#:
#: Checked live: the ``Ubisoft2`` board publishes ``og:site_name`` of
#: "Ubisoft", titles itself "Careers at Ubisoft", and its job pages name the
#: employer "Ubisoft" - while the opted-out ``Ubisoft`` slug no longer resolves
#: at all. The employer moved boards; the rule did not follow. Reading that as
#: permission would be honouring the letter of an opt-out while ignoring the
#: only thing it was written to say.
#:
#: Kept apart from ``OPTED_OUT_EMPLOYERS`` on purpose: that set is the vendor's
#: file, verbatim, and must stay auditable against it. This one is ours, and
#: each entry needs the kind of evidence recorded above.
OPTED_OUT_BY_IDENTITY: frozenset[str] = frozenset({"Ubisoft2"})


#: Employer boards read unless an operator configures otherwise. Override with
#: ``portal_configs.options["boards"]``, a list of slugs.
#:
#: Every slug here was fetched before being added: each returned a board page
#: carrying its own ``companyIdentifier``, and each was measured for United
#: States postings inside the 30-day window. A slug is a hypothesis until
#: proved - 23 of 70 plausible company names resolved - so nothing may be added
#: here by inference from a name.
#:
#: Employer names are deliberately absent. SmartRecruiters publishes the
#: organisation on the job page itself, so a board is addressed by slug alone
#: and cannot carry a stale company name - the same arrangement as
#: ``ashby.py``, and the opposite of ``workday.py``, whose payload offers only
#: a cost centre.
#:
#: Six boards were verified and are still **not** shipped, because a board that
#: costs more than it returns is a board that spends the leg's budget on
#: nothing. ``ClarkDavisAssociates`` (97 job pages), ``JobsForHumanity`` (65)
#: and ``prosidianconsulting`` (42) are staffing boards whose fuzzy search
#: answers almost any analyst query with hundreds of near-duplicate listings:
#: together they were 204 of 273 job-page fetches - 75% of the portal's entire
#: cost - and produced no qualifying job for either test resume.
#: ``AuthenticBrandsGroup`` and ``Wayfair`` return no cards at all.
#: ``Ubisoft2`` is excluded on the opt-out, not on cost - see
#: ``OPTED_OUT_BY_IDENTITY``. All six are pinned in
#: ``tests/test_smartrecruiters_boards.py`` so that restoring one is a
#: decision rather than an accident.
DEFAULT_BOARDS: tuple[str, ...] = (
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


def title_tokens(value: str | None) -> frozenset[str]:
    """Comparable word tokens from a job title or a query."""
    if not value:
        return frozenset()
    return frozenset(
        token
        for token in _WORD_RE.findall(value.casefold())
        if len(token) >= _MIN_TOKEN_LENGTH
    )


def title_matches(query: str, title: str) -> bool:
    """Whether a card title answers a search query.

    Every significant word of the query must appear in the title, so
    "Financial Analyst" reaches "Senior Financial Analyst" and "Financial
    Analyst, Business F&S" but not "Business Analyst".

    SmartRecruiters' own ``?search=`` is fuzzy and relevance-ranked - it
    answers "data analyst" with "Data Wrangler" - so this is what turns a broad
    server-side search into the project's same-role rule. The role gate
    downstream remains the authority (CLAUDE.md section 7).

    Character-for-character the rule ``greenhouse.py``, ``lever.py``,
    ``dayforce.py``, ``google_careers.py``, ``ashby.py`` and ``workday.py``
    each state for themselves, and stated here for the same reason they do: no
    adapter in this package imports another, so a portal can be changed or
    retired without disturbing the rest. ``tests/test_smartrecruiters_adapter.py``
    pins this to Greenhouse's answer over a shared table of cases, so the
    copies cannot drift apart unnoticed.

    It also earns its keep twice here. Every title it rejects is a job page
    this adapter never requests.
    """
    wanted = title_tokens(query)
    return bool(wanted) and wanted <= title_tokens(title)


def is_opted_out(slug: str) -> bool:
    """Whether this employer asked not to be crawled.

    Answers for the vendor's published list *and* for boards that belong to an
    employer on it under a slug the rule does not literally match - see
    ``OPTED_OUT_BY_IDENTITY``.

    Compared case-insensitively, because a slug typed into configuration will
    not always match the capitalisation the vendor published.
    """
    wanted = slug.strip().casefold()
    return wanted in {
        entry.casefold()
        for entry in (*OPTED_OUT_EMPLOYERS, *OPTED_OUT_BY_IDENTITY)
    }


def clean_text(value: str | None) -> str | None:
    """Markup stripped, entities decoded, whitespace collapsed."""
    if not value:
        return None
    text = html.unescape(_TAG_RE.sub("", value))
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip() or None


def company_identifier(page: str) -> str | None:
    """The employer SmartRecruiters says this page belongs to.

    The one reliable proof that a board is real. A slug that does not exist and
    an employer that has moved off the platform both answer 200 with a page
    that carries no identifier at all.
    """
    match = _COMPANY_ID_RE.search(page)
    return match.group(1).strip() if match else None


def parse_cards(page: str) -> list[tuple[str, str]]:
    """Every search result card on the page, as ``(url, title)``.

    Deduplicated by URL while preserving order: SmartRecruiters renders the
    same posting more than once when it is grouped under several departments.
    """
    seen: set[str] = set()
    cards: list[tuple[str, str]] = []
    for match in _CARD_RE.finditer(page):
        url = html.unescape(match.group("url")).strip()
        title = clean_text(match.group("title"))
        if not url or not title or url in seen:
            continue
        seen.add(url)
        cards.append((url, title))
    return cards


def job_identity(url: str) -> tuple[str, str] | None:
    """``(company slug, numeric id)`` read out of a job URL, or None."""
    match = _JOB_ID_RE.search(url)
    if match is None:
        return None
    return match.group(1), match.group(2)


def meta_itemprop(page: str, name: str) -> str | None:
    """One ``<meta itemprop="...">`` value."""
    match = re.search(
        r'<meta[^>]*itemprop="' + re.escape(name) + r'"[^>]*content="([^"]*)"',
        page,
        re.IGNORECASE,
    )
    return html.unescape(match.group(1)).strip() if match else None


def organization_name(page: str) -> str | None:
    """The employer, read from the page's ``hiringOrganization`` scope.

    Scoped deliberately. ``itemprop="name"`` appears more than once on these
    pages, and taking the first would sometimes return the location's name
    rather than the company's, so the search starts at the organisation's own
    ``itemscope``.
    """
    start = page.find('itemprop="hiringOrganization"')
    if start >= 0:
        name = meta_itemprop(page[start:], "name")
        if name:
            return name

    # The social metadata carries it too, and agrees where both are present.
    match = re.search(
        r'<meta[^>]*property="og:site_name"[^>]*content="([^"]*)"', page, re.IGNORECASE
    )
    return html.unescape(match.group(1)).strip() if match else None


def job_title(page: str) -> str | None:
    """The posting's own heading."""
    for match in re.finditer(r"<h1[^>]*>(.*?)</h1>", page, re.IGNORECASE | re.DOTALL):
        title = clean_text(match.group(1))
        if title:
            return title
    return None


def parse_date_posted(value: Any) -> datetime | None:
    """``datePosted`` as an aware timestamp, or None.

    SmartRecruiters publishes an ISO-8601 instant with a ``Z`` suffix. Anything
    that is not one - absent, empty, a bare date, nonsense - returns None, and
    the caller drops the listing rather than dating it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def country_name(page: str) -> str | None:
    return meta_itemprop(page, "addressCountry")


def is_us_posting(page: str) -> bool:
    """Whether the page's structured country is the United States.

    The adapter does not filter on this - ``format_location`` carries the
    country into the location string and the shared pipeline decides. It is
    exposed for tests and for honest logging of what was collected.
    """
    country = country_name(page)
    return bool(country) and country.strip().casefold() in US_COUNTRY_NAMES


def format_location(page: str) -> str | None:
    """A location string built from the job page's structured address.

    Board cards are not used for this, and cannot be: on some employers the
    card's only text is the employment type, so reading a country from it would
    be reading "Full-time" as a place. The job page publishes locality, region
    and country as separate microdata fields, and those are what the shared
    classifier gets.
    """
    parts = [
        meta_itemprop(page, "addressLocality"),
        meta_itemprop(page, "addressRegion"),
        meta_itemprop(page, "addressCountry"),
    ]
    joined = ", ".join(part for part in parts if part)
    return joined or None


def employment_type(page: str) -> str | None:
    """``itemprop="employmentType"``, spelled the dashboard's way.

    Genuinely structured on these pages rather than inferred from body text.
    An unrecognised value is passed through as published.
    """
    match = _EMPLOYMENT_TYPE_RE.search(page)
    published = clean_text(match.group(1)) if match else None
    if not published:
        return None
    return _EMPLOYMENT_TYPES.get(published.casefold(), published)


def job_details(page: str) -> tuple[str, ...]:
    """The labelled chips under the title, minus the employment type.

    Department, work environment, pay grade and whatever else an employer
    chose to publish. Kept because they are the employer's own words: "Work
    Environment: Hybrid" is where a work mode comes from on this portal, and
    surfacing it as a labelled line lets the shared ``detect_work_mode`` read
    it without this adapter deciding what it means.
    """
    kept: list[str] = []
    for match in _JOB_DETAIL_RE.finditer(page):
        fragment = match.group(0)
        if "employmentType" in fragment:
            continue
        text = clean_text(match.group(1))
        if text:
            kept.append(text)
    return tuple(kept)


def html_to_text(value: str | None) -> str | None:
    """Flatten a fragment of job-page markup into readable plain text."""
    if not value or not value.strip():
        return None

    text = _LIST_ITEM_RE.sub("\n- ", value)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return _BLANK_RUN_RE.sub("\n\n", text).strip() or None


def build_description(page: str) -> str | None:
    """The posting's full published text, sections intact.

    SmartRecruiters marks each section up separately - Company Description, Job
    Description, Qualifications, Additional Information - and those headings
    are what the matching layer reads, so they survive as heading lines rather
    than being flattened into one paragraph. Nothing is truncated.

    The employer's detail chips are prepended as labelled lines, because
    ``RawJob`` has no field for a work arrangement and adding one would change
    the adapter contract for every other portal - the convention ``ashby.py``
    established.
    """
    container = _SECTIONS_RE.search(page)
    if container is not None:
        sections = _SECTION_RE.findall(container.group(1))
        body_source = "\n".join(sections) if sections else container.group(1)
    else:
        sections = _SECTION_RE.findall(page)
        if not sections:
            return None
        body_source = "\n".join(sections)

    body = html_to_text(body_source)
    if not body:
        return None

    details = job_details(page)
    if not details:
        return body
    return "\n\n".join([*details, body])


def apply_url(page: str) -> str | None:
    """The employer's published application link, or None.

    Read, never built. SmartRecruiters' apply destination carries a publication
    UUID that appears nowhere else on the page, so there is nothing to
    construct it from and a guessed URL would send the user to a 404.
    """
    match = _APPLY_RE.search(page)
    return html.unescape(match.group(1)).strip() if match else None


class SmartRecruitersPortal(BasePortal):
    """Reads each configured employer's SmartRecruiters board."""

    slug: ClassVar[str] = "smartrecruiters"
    display_name: ClassVar[str] = "SmartRecruiters"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._boards = self._configured_boards(self.options)
        self._policy = HttpPolicy.from_options(self.options)
        self._max_concurrent = self._configured_concurrency(self.options)

        #: One client per host. The two hosts are paced independently, so job
        #: pages for boards already read are fetched while the next board is
        #: still being searched.
        self._careers = PortalHttpClient(portal=self.slug, policy=self._policy)
        self._jobs = PortalHttpClient(portal=self.slug, policy=self._policy)

        #: Search pages already read, keyed by board, query and page.
        self._searches: dict[tuple[str, str, int], list[tuple[str, str]]] = {}
        #: Job pages already read. A posting that answers several same-role
        #: variations must cost exactly one request.
        self._jobs_cache: dict[str, RawJob | None] = {}
        #: In-flight job fetches, so two variations queued together do not both
        #: issue the request before either has finished.
        self._inflight: dict[str, asyncio.Task[RawJob | None]] = {}

    @staticmethod
    def _configured_boards(options: dict[str, Any]) -> tuple[str, ...]:
        """Board slugs from configuration, falling back to the defaults.

        An opted-out employer is refused here as well as at request time, so a
        configuration that names one cannot quietly take effect.
        """
        configured = options.get("boards")
        if isinstance(configured, str):
            configured = [configured]

        slugs: list[str] = []
        if isinstance(configured, list | tuple):
            for entry in configured:
                # Accept the mapping form too, so an operator who copies the
                # Lever or Workday shape is not silently ignored.
                if isinstance(entry, dict):
                    entry = entry.get("slug")
                if isinstance(entry, str) and entry.strip():
                    slugs.append(entry.strip())

        chosen = tuple(slugs) if slugs else DEFAULT_BOARDS
        permitted = tuple(slug for slug in chosen if not is_opted_out(slug))

        for slug in chosen:
            if is_opted_out(slug):
                logger.warning(
                    "smartrecruiters_board_opted_out",
                    portal=SmartRecruitersPortal.slug,
                    board=slug,
                )
        return permitted

    @staticmethod
    def _configured_concurrency(options: dict[str, Any]) -> int:
        raw = options.get("max_concurrent_jobs")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return DEFAULT_MAX_CONCURRENT_JOBS
        return max(1, raw)

    @property
    def boards(self) -> tuple[str, ...]:
        return self._boards

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # The search page offers no date filter; freshness is applied by
            # the caller against posted_at, which datePosted makes exact.
            supports_date_filter=False,
            # `search` already returns the full posting text: the job page it
            # opens for the date carries the description as well, so there is
            # nothing left for a second pass to fetch.
            supports_description_fetch=False,
            supports_pagination=True,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- URLs --------------------------------------------------------------

    def board_url(self, slug: str) -> str:
        return f"{CAREERS_BASE}/{slug}"

    def search_url(self, slug: str, query: str, page: int = 1) -> str:
        """The employer's board, filtered server-side by ``query``."""
        url = f"{CAREERS_BASE}/{slug}?search={quote(query)}"
        return url if page <= 1 else f"{url}&page={page}"

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings matching the request's same-role title variations.

        Boards are read one at a time - they share a host, so there is nothing
        to gain by asking for several at once - but each board's matching job
        pages start downloading from the *other* host immediately, so the two
        run concurrently rather than one after the other.
        """
        if not self._boards:
            raise PortalError(
                "No SmartRecruiters boards are configured.", portal=self.slug
            )

        pending: dict[str, asyncio.Task[RawJob | None]] = {}
        failures: list[str] = []
        semaphore = asyncio.Semaphore(self._max_concurrent)

        try:
            for slug in self._boards:
                if is_opted_out(slug):
                    # Belt and braces: `_configured_boards` already removed
                    # these. Checked again here so that no future path can
                    # reach a request for an employer that opted out.
                    continue
                if len(pending) >= request.limit:
                    break

                try:
                    cards = await self._read_board(slug, request)
                except (PortalBlockedError, PortalRateLimitedError):
                    raise
                except PortalError as exc:
                    logger.info(
                        "smartrecruiters_board_unavailable",
                        portal=self.slug,
                        board=slug,
                        error=exc.message,
                    )
                    failures.append(slug)
                    continue

                for url, title in cards:
                    if len(pending) >= request.limit:
                        break
                    if url in pending:
                        continue
                    if not any(
                        title_matches(query, title) for query in request.queries
                    ):
                        continue
                    pending[url] = asyncio.create_task(
                        self._read_job(url, semaphore=semaphore)
                    )

            if failures and len(failures) == len(self._boards):
                raise PortalError(
                    f"No SmartRecruiters board could be read "
                    f"({len(failures)} attempted).",
                    portal=self.slug,
                )

            gathered = await asyncio.gather(*pending.values(), return_exceptions=True)
        finally:
            for task in pending.values():
                if not task.done():
                    task.cancel()

        jobs: list[RawJob] = []
        for outcome in gathered:
            if isinstance(outcome, PortalBlockedError | PortalRateLimitedError):
                raise outcome
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, PortalError):
                    raise outcome
                continue
            if outcome is not None:
                jobs.append(outcome)

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            has_more=False,
            next_cursor=None,
            # Collected unfiltered; the caller applies the freshness window.
            window_applied_by_portal=False,
        )

    async def _read_board(
        self, slug: str, request: PortalSearchRequest
    ) -> list[tuple[str, str]]:
        """Every card this board returns for the request's queries.

        Cached per (board, query, page) for the life of the run.
        """
        found: dict[str, str] = {}
        for query in request.queries:
            for url, title in await self._search_board(slug, query):
                found.setdefault(url, title)
        return list(found.items())

    async def _search_board(self, slug: str, query: str) -> list[tuple[str, str]]:
        """One board, one query, with bounded pagination."""
        collected: dict[str, str] = {}

        for page in range(1, MAX_SEARCH_PAGES + 1):
            cached = self._searches.get((slug, query, page))
            if cached is None:
                body = await self._careers.get_text(self.search_url(slug, query, page))
                identifier = company_identifier(body)
                if identifier is None or identifier.casefold() != slug.casefold():
                    # The soft failure. A slug that does not exist redirects to
                    # the product root, and an employer that has moved serves
                    # its own site; neither carries this board's identifier.
                    raise PortalError(
                        f"SmartRecruiters board {slug} did not answer as itself "
                        f"(page identifies as {identifier!r}).",
                        portal=self.slug,
                    )
                cached = parse_cards(body)
                self._searches[(slug, query, page)] = cached

            fresh = [item for item in cached if item[0] not in collected]
            collected.update(dict(cached))

            # The search returns its whole result set on one page in every
            # case measured, so a second page is asked for only when the first
            # was implausibly full, and abandoned as soon as it adds nothing.
            if len(cached) < PAGINATION_THRESHOLD or not fresh:
                break

        return list(collected.items())

    async def _read_job(
        self, url: str, *, semaphore: asyncio.Semaphore
    ) -> RawJob | None:
        """One posting's own page, which is where the date and the text live.

        Returns None when the page cannot be read or carries no publication
        date. A listing whose date is unknown could not pass the freshness gate
        anyway, and dropping it here keeps a dateless record out of the
        pipeline.
        """
        if url in self._jobs_cache:
            return self._jobs_cache[url]

        existing = self._inflight.get(url)
        if existing is not None:
            return await asyncio.shield(existing)

        async def fetch() -> RawJob | None:
            async with semaphore:
                if url in self._jobs_cache:
                    return self._jobs_cache[url]
                try:
                    page = await self._jobs.get_text(url)
                except (PortalBlockedError, PortalRateLimitedError):
                    raise
                except PortalError as exc:
                    logger.info(
                        "smartrecruiters_job_unavailable",
                        portal=self.slug,
                        job=url,
                        error=exc.message,
                    )
                    self._jobs_cache[url] = None
                    return None

                raw = self._to_raw_job(page, url=url)
                self._jobs_cache[url] = raw
                return raw

        task = asyncio.ensure_future(fetch())
        self._inflight[url] = task
        try:
            return await task
        finally:
            self._inflight.pop(url, None)

    def _to_raw_job(self, page: str, *, url: str) -> RawJob | None:
        """Map one SmartRecruiters posting onto the shared RawJob contract."""
        identity = job_identity(url)
        if identity is None:
            logger.info(
                "smartrecruiters_job_without_identity", portal=self.slug, job=url
            )
            return None
        slug, job_id = identity

        title = job_title(page)
        if not title:
            return None

        posted_at = parse_date_posted(meta_itemprop(page, "datePosted"))
        if posted_at is None:
            logger.info(
                "smartrecruiters_job_without_publication_date",
                portal=self.slug,
                job=url,
            )
            return None

        description = build_description(page)
        if description is None:
            return None

        return RawJob(
            external_id=job_id,
            source=self.slug,
            title=title,
            # Published on the page, so no board has to be told who owns it.
            company=organization_name(page),
            location=format_location(page),
            description=description,
            source_url=url,
            # Read, never constructed: the publication UUID exists nowhere
            # else, so a built URL would be a guess.
            application_url=apply_url(page),
            posted_at=posted_at,
            employment_type=employment_type(page),
            raw_payload=declare_application_type(
                {
                    "board": slug,
                    "jobId": job_id,
                    "industry": meta_itemprop(page, "industry"),
                    "details": list(job_details(page)),
                    "addressCountry": meta_itemprop(page, "addressCountry"),
                    "addressLocality": meta_itemprop(page, "addressLocality"),
                    "addressRegion": meta_itemprop(page, "addressRegion"),
                    "postalCode": meta_itemprop(page, "postalCode"),
                    "streetAddress": meta_itemprop(page, "streetAddress"),
                },
                ApplicationType.LONG_APPLICATION,
            ),
        )

    # --- Contract ----------------------------------------------------------

    async def fetch_description(self, job: RawJob) -> str | None:
        """The full posting text already arrived with the listing."""
        return job.description

    async def validate_url(self, url: str) -> bool:
        return await self._jobs.head_ok(url)

    async def health_check(self) -> PortalHealth:
        """Reachability of the board host, via the first configured board."""
        if not self._boards:
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail="No SmartRecruiters boards are configured.",
            )
        try:
            await self._search_board(self._boards[0], "analyst")
        except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=f"Board page did not respond: {type(exc).__name__}.",
            )
        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))


__all__ = [
    "CAREERS_BASE",
    "DEFAULT_BOARDS",
    "DEFAULT_MAX_CONCURRENT_JOBS",
    "FORBIDDEN_API_HOST",
    "JOBS_BASE",
    "MAX_SEARCH_PAGES",
    "OPTED_OUT_EMPLOYERS",
    "PAGINATION_THRESHOLD",
    "US_COUNTRY_NAMES",
    "SmartRecruitersPortal",
    "apply_url",
    "build_description",
    "clean_text",
    "company_identifier",
    "country_name",
    "employment_type",
    "format_location",
    "html_to_text",
    "is_opted_out",
    "is_us_posting",
    "job_details",
    "job_identity",
    "job_title",
    "meta_itemprop",
    "organization_name",
    "parse_cards",
    "parse_date_posted",
    "title_matches",
    "title_tokens",
]
