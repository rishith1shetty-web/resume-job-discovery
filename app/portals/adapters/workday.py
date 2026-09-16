"""Workday adapter (``https://<tenant>.wd<N>.myworkdayjobs.com/``).

Access surface
--------------
Workday career sites draw themselves from a public endpoint on the employer's
own tenant host, and that is what this adapter reads::

    POST /wday/cxs/<tenant>/<site>/jobs      {"limit", "offset", "searchText"}
    GET  /wday/cxs/<tenant>/<site><externalPath>

No account, no API key, no cookie, no CSRF token and no session: verified live
against thirty unrelated tenants. ``app.careers.listings`` already asks the
same endpoint the same question for career-page evidence; this module is the
portal that turns the answer into listings, and the two deliberately do not
share code - one produces a ``ListingProbe``, the other a ``RawJob``, and
adapters in this package import neither each other nor other subsystems'
probes.

Robots, asked per tenant at run time
------------------------------------
Every other adapter here resolves robots at design time, because every other
adapter talks to one host that was read once and documented in
``app.services.portal_seed``. Workday cannot: each employer is a *different
host* publishing its *own* file, so the question is asked per tenant, per run,
through ``app.careers.robots`` - the same parser career-page validation uses,
with the same status conventions (404/410 means no restrictions published,
anything else unreadable means decline).

Measured on the shipped tenants, every file is readable and says the same kind
of thing::

    Sitemap: https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/siteMap.xml
    User-agent: *
    Allow: /NVIDIAExternalCareerSite/
    Disallow: /talentcommunity/
    Disallow: /refreshFacet/

Both the career site path and the CXS path are permitted, and the narrow
internal paths are never touched. But tenants *do* close individual sites -
``mastercard`` disallows ``/Public_Posting_Site/`` and ``pfizer``
``/InternalJobs/`` - so the check is against **this tenant's site and this
tenant's CXS path**, not merely against the host. A tenant whose policy
declines is skipped, never worked around.

One trap worth recording: these hosts serve ``robots.txt`` through a static
file handler that honours ``Accept``. Asking for ``application/json`` alone
returns **406**, which would read as "has a policy, could not retrieve it" and
silently decline every tenant. ``get_optional_text`` sends the client's normal
broad ``Accept``, which is why the JSON headers used for the CXS calls are
never applied to the policy fetch.

Why the employer name is configured
-----------------------------------
This is the one place Workday's data cannot be trusted, and it is why
``WorkdayTenant`` carries a ``display_name`` when ``ashby.py`` needs no such
thing. ``hiringOrganization.name`` is a Workday *legal entity or cost centre*,
not a brand. Measured live on the shipped tenants::

    cvshealth   -> "1120 Hook-SupeRx, L.L.C."
    usbank      -> "300 U.S. Bank National Association"
    allstate    -> "National General Management Corp"
    ally        -> "4001 Motors Insurance Corp."
    dow         -> "ROHM & HAAS TEXAS INC."
    capitalone  -> ""

Shipping those would put "1120 Hook-SupeRx, L.L.C." on the dashboard, and -
worse - fragment one employer across several canonical keys, because
``build_canonical_key`` groups on the normalised company. So the configured
``display_name`` is the employer identity, and the raw entity is kept in
``raw_payload`` for debugging and nothing else. The cost is the stale-name
risk ``ashby.py`` was built to avoid, accepted deliberately here because the
alternative is demonstrably wrong.

Cost, and why this portal is not slow
-------------------------------------
Thirty tenants against a 90-second ceiling on one ``search`` call is the whole
engineering problem, and four things solve it.

**Each tenant is its own host**, so each gets its own ``PortalHttpClient`` and
therefore its own rate limiter. Pacing every request to *one* employer at the
configured rate is politeness; pacing across thirty unrelated employers as if
they were one server is not, and it would cost a minute of pure sleeping per
pass. Every client is built from the same ``HttpPolicy``, so nothing is
relaxed - the limit is applied per host, which is what it describes.

**Tenants are read concurrently**, bounded by ``max_concurrent_tenants``.

**Everything is cached for the life of the run**: the robots policy per host,
the list page per (tenant, query, offset), and the job detail per URL. The
collector calls ``search`` once per same-role variation, so a posting that
answers both "financial analyst" and "senior financial analyst" is fetched
exactly once - ``tests/test_workday_adapter.py`` pins that with a request
count rather than trusting it.

**Stale listings are dropped before they cost a request.** The list response
dates each row only as a relative label, but "Posted 30+ Days Ago" is already
outside the 30-day window whatever the exact date, so those rows are discarded
without opening the detail. Measured on the live funnel that removed 53 of 137
candidates for one resume and 119 of 193 for the other.

The list endpoint caps ``limit`` at 20 - larger values return zero rows and a
null total - so breadth comes from paging, and an out-of-range ``offset``
returns rows rather than an empty page, so paging stops on the reported
``total`` and never on an empty response.

Dates
-----
``postedOn`` is a relative label; ``startDate`` is the real ISO date, and it is
what the site itself renders as that label - checked on 42 postings across six
tenants with no disagreement. ``startDate`` is therefore ``posted_at``. There
is no separate modified or refresh timestamp to confuse it with, and a missing
or unparseable value leaves ``posted_at`` unset so the freshness gate rejects
the listing rather than assuming it is recent.

This adapter collects and normalises. It does not score, deduplicate, rank or
apply to anything.
"""

from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, ClassVar

from app.careers.robots import RobotsPolicy, origin_of, robots_url
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
    out_of_time,
)
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
)
from app.portals.http import HttpPolicy, PortalHttpClient

logger = get_logger(__name__)

#: Largest page the list endpoint actually serves. Asking for more returns an
#: empty ``jobPostings`` and a null ``total`` - measured, not assumed - so this
#: is a hard property of the endpoint rather than a tuning choice.
PAGE_SIZE = 20

#: Pages walked per (tenant, query). Workday orders by relevance, so the
#: matches for a same-role title are at the front; this bounds a tenant with
#: twenty thousand openings to a handful of requests.
MAX_PAGES_PER_QUERY = 3

#: Tenants read at once. Each has its own host and its own rate limiter, so
#: this bounds concurrency across employers, not requests to any one of them.
DEFAULT_MAX_CONCURRENT_TENANTS = 16

#: The relative label Workday uses for anything at or past thirty days. Such a
#: row cannot pass the freshness gate at any exact date, so it is dropped
#: before its detail page is ever requested.
STALE_POSTED_MARKER = "30+"

#: How Workday spells the United States in ``country``. The alpha-2 code is
#: preferred; the descriptor is the fallback for the few payloads that carry
#: only a name.
US_COUNTRY_CODES = frozenset({"us", "usa"})
US_COUNTRY_NAMES = frozenset(
    {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
)

#: Workday's own workplace vocabulary, canonicalised only where it is
#: unambiguous. Anything else - Salesforce's "Office - Flexible", say - is
#: passed through as published, and the shared ``detect_work_mode`` then finds
#: no keyword and returns None. That is the intended outcome: an unrecognised
#: arrangement should read as unknown, not be forced into a guess.
_WORKPLACE_TYPES = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "office": "Onsite",
    "onsite": "Onsite",
    "on site": "Onsite",
}

#: Workday publishes time type with a space; the dashboard uses a hyphen.
_EMPLOYMENT_TYPES = {
    "full time": "Full-time",
    "part time": "Part-time",
    "fixed term": "Fixed-term",
    "intern": "Intern",
    "contract": "Contract",
}

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[a-z0-9]+")
_BLOCK_RE = re.compile(
    r"</(p|div|li|ul|ol|h[1-6]|tr|table|section|br)\s*>|<br\s*/?>", re.IGNORECASE
)
_LIST_ITEM_RE = re.compile(r"<li[^>]*>", re.IGNORECASE)
_RELATIVE_DAYS_RE = re.compile(r"posted\s+(\d+)\s*\+?\s*days?\s+ago", re.IGNORECASE)

#: Single letters match nearly every title and carry no signal: "FP&A Analyst"
#: must require "fp" and "analyst", not "a".
_MIN_TOKEN_LENGTH = 2


@dataclass(frozen=True, slots=True)
class WorkdayTenant:
    """One employer's career site on their own Workday tenant.

    Four facts, all verified before shipping: the tenant host answers, the site
    exists on it, its robots policy permits the CXS path, and the tenant is the
    employer the ``display_name`` claims. That last one is not pedantry -
    ``emerson`` resolves to Emerson College, not Emerson Electric.
    """

    tenant: str
    #: The data-centre number in ``wd<N>``, as a string because it is part of a
    #: hostname rather than a quantity.
    wd: str
    site: str
    #: The employer brand. Configured, never read from the payload - see the
    #: module docstring for the measurements that forced this.
    display_name: str

    @property
    def host(self) -> str:
        return f"https://{self.tenant}.wd{self.wd}.myworkdayjobs.com"

    @property
    def key(self) -> str:
        """Identifies one tenant *and site*: a tenant may publish several."""
        return f"{self.tenant}/wd{self.wd}/{self.site}"

    @property
    def cxs_base(self) -> str:
        return f"{self.host}/wday/cxs/{self.tenant}/{self.site}"

    @property
    def jobs_url(self) -> str:
        return f"{self.cxs_base}/jobs"

    @property
    def site_url(self) -> str:
        """The human career site, which is what robots.txt names in `Allow:`.

        Built with a trailing slash on purpose. Workday writes these rules as
        directory prefixes - ``Disallow: /Public_Posting_Site/`` - and a robots
        prefix match is literal, so testing the bare ``/Public_Posting_Site``
        matches nothing and a closed site reads as open. That is the difference
        between honouring a tenant's refusal and walking straight past it.
        """
        return f"{self.host}/{self.site}/"

    def detail_url(self, external_path: str) -> str:
        return f"{self.cxs_base}{external_path}"

    def as_dict(self) -> dict[str, str]:
        return {
            "tenant": self.tenant,
            "wd": self.wd,
            "site": self.site,
            "display_name": self.display_name,
        }


#: Employer tenants read unless an operator configures otherwise. Override with
#: ``portal_configs.options["tenants"]``, a list of
#: ``{tenant, wd, site, display_name}`` mappings.
#:
#: Every entry was verified live before being added: the host resolved, its
#: ``robots.txt`` returned 200 and permitted both the site and the CXS path,
#: the site returned a posting total, and the employer was confirmed against
#: the legal entity the tenant itself returns. A tenant name is a hypothesis
#: until proved - 35 of 99 plausible company names resolved - so nothing may be
#: added here by inference from a company name.
#:
#: ``display_name`` is the employer brand and is deliberately configuration.
#: See the module docstring: Workday's own organisation name is a cost centre.
DEFAULT_TENANTS: tuple[WorkdayTenant, ...] = (
    WorkdayTenant("usbank", "1", "US_Bank_Careers", "U.S. Bank"),
    WorkdayTenant("capitalone", "12", "Capital_One", "Capital One"),
    WorkdayTenant("cvshealth", "1", "CVS_Health_Careers", "CVS Health"),
    WorkdayTenant("target", "5", "targetcareers", "Target"),
    WorkdayTenant("nationwide", "1", "Nationwide_Career", "Nationwide"),
    WorkdayTenant("allstate", "5", "allstate_careers", "Allstate"),
    WorkdayTenant("sysco", "5", "syscocareers", "Sysco"),
    WorkdayTenant("salesforce", "12", "External_Career_Site", "Salesforce"),
    WorkdayTenant("nvidia", "5", "NVIDIAExternalCareerSite", "NVIDIA"),
    WorkdayTenant("adobe", "5", "external_experienced", "Adobe"),
    WorkdayTenant("cigna", "5", "cignacareers", "The Cigna Group"),
    WorkdayTenant("humana", "5", "Humana_External_Career_Site", "Humana"),
    WorkdayTenant("travelers", "5", "External", "Travelers"),
    WorkdayTenant("pnc", "5", "External", "PNC"),
    WorkdayTenant("ally", "1", "Ally", "Ally"),
    WorkdayTenant("fiserv", "5", "EXT", "Fiserv"),
    WorkdayTenant("mastercard", "1", "CorporateCareers", "Mastercard"),
    WorkdayTenant("boeing", "1", "EXTERNAL_CAREERS", "Boeing"),
    WorkdayTenant("3m", "1", "Search", "3M"),
    WorkdayTenant("leidos", "5", "External", "Leidos"),
    WorkdayTenant("amgen", "1", "Careers", "Amgen"),
    WorkdayTenant("gilead", "1", "gileadcareers", "Gilead Sciences"),
    WorkdayTenant("pfizer", "1", "PfizerCareers", "Pfizer"),
    WorkdayTenant("chevron", "5", "jobs", "Chevron"),
    WorkdayTenant("dow", "1", "ExternalCareers", "Dow"),
    WorkdayTenant("dupont", "5", "Jobs", "DuPont"),
    WorkdayTenant("dollartree", "5", "dollartreeus", "Dollar Tree"),
    WorkdayTenant("workday", "5", "Workday", "Workday"),
    WorkdayTenant("unilever", "3", "Unilever_Experienced_Professionals", "Unilever"),
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
    """Whether a listing title answers a search query.

    Every significant word of the query must appear in the title, so
    "Financial Analyst" reaches "Senior Financial Analyst" and "Financial
    Analyst, Business F&S" but not "Business Analyst".

    Workday's ``searchText`` is fuzzy and relevance-ranked - it answers "data
    analyst" with "Data Wrangler" - so this is what turns a broad server-side
    search into the project's same-role rule. The role gate downstream remains
    the authority (CLAUDE.md section 7).

    Character-for-character the rule ``greenhouse.py``, ``lever.py``,
    ``dayforce.py``, ``google_careers.py`` and ``ashby.py`` each state for
    themselves, and stated here for the same reason they do: no adapter in this
    package imports another, so a portal can be changed or retired without
    disturbing the rest. ``tests/test_workday_adapter.py`` pins this to
    Greenhouse's answer over a shared table of cases, so the copies cannot
    drift apart unnoticed.

    It also earns its keep twice here: every title it rejects is a detail page
    this adapter never requests.
    """
    wanted = title_tokens(query)
    return bool(wanted) and wanted <= title_tokens(title)


def is_stale_label(posted_on: str | None) -> bool:
    """Whether a list row's relative date already puts it outside the window.

    ``"Posted 30+ Days Ago"`` is Workday's bucket for everything at or past
    thirty days. Whatever its exact ``startDate``, such a listing cannot pass
    ``is_eligible_age``, so it is dropped before its detail page is requested.

    Deliberately narrow. Only the explicit "30+" bucket is treated as stale; a
    row saying "Posted 28 Days Ago" is kept and dated exactly from its detail,
    because guessing from the label is how a listing inside the window gets
    thrown away.
    """
    if not posted_on:
        return False
    return STALE_POSTED_MARKER in posted_on


def relative_label_days(posted_on: str | None) -> int | None:
    """The day count a relative label states, when it states one.

    Used only to corroborate ``startDate`` in tests and logs - never to build a
    date. A label is a bucket, and a bucket is not a publication date.
    """
    if not posted_on:
        return None
    lowered = posted_on.casefold()
    if "today" in lowered:
        return 0
    if "yesterday" in lowered:
        return 1
    match = _RELATIVE_DAYS_RE.search(posted_on)
    return int(match.group(1)) if match else None


def parse_start_date(value: Any) -> datetime | None:
    """Workday's ``startDate`` as an aware timestamp, or None.

    The field is a plain ISO date, so it is read at midnight UTC. Anything that
    is not such a date - absent, empty, a full timestamp, a number, nonsense -
    returns None, and the caller drops the listing rather than dating it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        return None
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def html_to_text(value: Any) -> str | None:
    """Flatten Workday's description HTML into readable plain text.

    Block boundaries become newlines and list items gain a bullet, so the
    sections the matching layer reads - responsibilities, qualifications,
    requirements, benefits - survive as separate lines rather than collapsing
    into one paragraph.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = _LIST_ITEM_RE.sub("\n- ", value)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")

    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return _BLANK_RUN_RE.sub("\n\n", text).strip() or None


def country_code(posting: dict[str, Any]) -> str | None:
    """The posting's structured country, preferring the ISO alpha-2 code.

    Read only from ``jobRequisitionLocation.country`` and the top-level
    ``country``; never inferred from a location string.
    """
    location = posting.get("jobRequisitionLocation")
    if isinstance(location, dict):
        country = location.get("country")
        if isinstance(country, dict):
            code = country.get("alpha2Code")
            if isinstance(code, str) and code.strip():
                return code.strip()
            name = country.get("descriptor")
            if isinstance(name, str) and name.strip():
                return name.strip()

    country = posting.get("country")
    if isinstance(country, dict):
        for field in ("alpha2Code", "descriptor"):
            value = country.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def country_name(posting: dict[str, Any]) -> str | None:
    """The posting's country as Workday names it, for the location string."""
    location = posting.get("jobRequisitionLocation")
    if isinstance(location, dict):
        country = location.get("country")
        if isinstance(country, dict):
            name = country.get("descriptor")
            if isinstance(name, str) and name.strip():
                return name.strip()

    country = posting.get("country")
    if isinstance(country, dict):
        name = country.get("descriptor")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def is_us_posting(posting: dict[str, Any]) -> bool:
    """Whether the posting's *structured* country is the United States.

    Only the primary requisition location is structured; ``additionalLocations``
    is a list of bare strings with no country on it. So this answers on the one
    published fact and nothing else, which makes a multi-location role count as
    United States only when Workday itself put it there. Under-inclusive by
    choice: reading a country out of "Canada - Montreal" would be inference,
    and this project does not infer countries from strings.

    The adapter does not filter on this - ``format_location`` carries the
    country into the location string and the shared pipeline decides. It is
    exposed for tests and for the honest logging of what was collected.
    """
    code = country_code(posting)
    if not code:
        return False
    folded = code.strip().casefold()
    return folded in US_COUNTRY_CODES or folded in US_COUNTRY_NAMES


def format_location(posting: dict[str, Any]) -> str | None:
    """A location string the shared US classifier can actually read.

    Workday's own descriptors are tenant-specific and wildly inconsistent -
    ``"US, CA, Santa Clara"``, ``"MS - Kosciusko"``, ``"VA-Chesapeake"``,
    ``"Work At Home-Connecticut"``, ``"7000 Target Pkwy N,NCD-0000"`` - and two
    of those defeat ``classify_us_location`` on their own: ``"VA-Chesapeake"``
    and the bare street address both come back unclassified, which would throw
    away real United States jobs.

    So the published country descriptor is appended. That is not a guess: it is
    the structured field Workday publishes beside the descriptor, and adding it
    lets the existing classifier answer from an explicit country instead of
    trying to recognise a state abbreviation in an arbitrary format. Non-US
    postings stay non-US for exactly the same reason.

    ``us_locations.py`` is untouched; the adapter formats, the pipeline decides.
    """
    descriptor = ""
    location = posting.get("jobRequisitionLocation")
    if isinstance(location, dict):
        value = location.get("descriptor")
        if isinstance(value, str):
            descriptor = value.strip()

    if not descriptor:
        value = posting.get("location")
        if isinstance(value, str):
            descriptor = value.strip()

    country = country_name(posting)

    if descriptor and country:
        # Already stated: "United States - Illinois - Chicago, United States"
        # reads badly and adds nothing.
        if descriptor.casefold().endswith(country.casefold()):
            return descriptor
        return f"{descriptor}, {country}"
    return descriptor or country or None


def additional_locations(posting: dict[str, Any]) -> tuple[str, ...]:
    """The other places a multi-location role is open, as published.

    Bare strings with no structured country, which is why they inform the
    description and never the United States decision.
    """
    values = posting.get("additionalLocations")
    if not isinstance(values, list):
        return ()
    return tuple(
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    )


def workplace_label(posting: dict[str, Any]) -> str | None:
    """Workday's ``remoteType``, canonicalised only where it is unambiguous."""
    value = posting.get("remoteType")
    if not isinstance(value, str) or not value.strip():
        return None
    published = value.strip()
    return _WORKPLACE_TYPES.get(published.casefold(), published)


def employment_type(posting: dict[str, Any]) -> str | None:
    """``timeType``, spelled the way the dashboard spells it."""
    value = posting.get("timeType")
    if not isinstance(value, str) or not value.strip():
        return None
    published = value.strip()
    return _EMPLOYMENT_TYPES.get(published.casefold(), published)


def build_description(posting: dict[str, Any]) -> str | None:
    """The posting's full text, with what the contract cannot carry prepended.

    ``RawJob`` has no work-mode field and no secondary-location field, and
    adding either would change the adapter contract for every other portal. So
    both are surfaced as labelled lines at the top, which is the convention
    ``ashby.py`` established and which the shared ``detect_work_mode`` reads.

    Nothing is truncated, and the legal entity is deliberately not injected:
    it is not role-requirement text and would alter the input scoring sees.
    """
    body = html_to_text(posting.get("jobDescription"))
    if not body:
        return None

    parts: list[str] = []
    label = workplace_label(posting)
    if label:
        parts.append(f"Workplace type: {label}")

    others = additional_locations(posting)
    if others:
        parts.append(f"Additional locations: {', '.join(others)}")

    parts.append(body)
    return "\n\n".join(parts)


@dataclass(slots=True)
class _Candidate:
    """One list row that survived the title gate and the stale-label drop."""

    tenant: WorkdayTenant
    title: str
    external_path: str
    posted_on: str | None
    locations_text: str | None

    @property
    def url(self) -> str:
        return self.tenant.detail_url(self.external_path)


class WorkdayPortal(BasePortal):
    """Reads each configured employer's Workday career site."""

    slug: ClassVar[str] = "workday"
    display_name: ClassVar[str] = "Workday"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._tenants = self._configured_tenants(self.options)
        self._policy = HttpPolicy.from_options(self.options)
        self._max_concurrent = self._configured_concurrency(self.options)

        #: One client per host, so the configured rate limit paces each
        #: employer's own server rather than being spent across thirty of them.
        self._clients: dict[str, PortalHttpClient] = {}
        #: Robots policy per origin, asked once per run.
        self._robots: dict[str, RobotsPolicy] = {}
        #: List pages already read, keyed by tenant, query and offset.
        self._lists: dict[tuple[str, str, int], tuple[list[dict[str, Any]], int]] = {}
        #: Detail pages already read. A posting matching several same-role
        #: variations must cost exactly one request.
        self._details: dict[str, RawJob | None] = {}

    @staticmethod
    def _configured_tenants(options: dict[str, Any]) -> tuple[WorkdayTenant, ...]:
        """Tenants from configuration, falling back to the verified defaults.

        Every one of the four fields is required. An entry missing any of them
        is dropped rather than half-built: a tenant without a ``display_name``
        would publish listings under a blank employer, and a tenant without a
        site cannot be addressed at all.
        """
        configured = options.get("tenants")
        if not isinstance(configured, list | tuple):
            return DEFAULT_TENANTS

        tenants: list[WorkdayTenant] = []
        for entry in configured:
            if not isinstance(entry, dict):
                continue
            values = {
                field: str(entry.get(field, "")).strip()
                for field in ("tenant", "wd", "site", "display_name")
            }
            if not all(values.values()):
                logger.warning(
                    "workday_tenant_config_incomplete",
                    portal=WorkdayPortal.slug,
                    entry=entry,
                )
                continue
            # Accept "wd5" as well as "5": the number is what the host needs,
            # and an operator copying it out of a URL will bring the prefix.
            wd = values["wd"].casefold().removeprefix("wd")
            if not wd.isdigit():
                logger.warning(
                    "workday_tenant_config_bad_wd",
                    portal=WorkdayPortal.slug,
                    entry=entry,
                )
                continue
            tenants.append(
                WorkdayTenant(
                    tenant=values["tenant"],
                    wd=wd,
                    site=values["site"],
                    display_name=values["display_name"],
                )
            )

        return tuple(tenants) if tenants else DEFAULT_TENANTS

    @staticmethod
    def _configured_concurrency(options: dict[str, Any]) -> int:
        raw = options.get("max_concurrent_tenants")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return DEFAULT_MAX_CONCURRENT_TENANTS
        return max(1, raw)

    @property
    def tenants(self) -> tuple[WorkdayTenant, ...]:
        return self._tenants

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # No tenant published a date facet, and the list rows carry only a
            # relative bucket. Freshness is applied by the caller against
            # posted_at, which startDate makes exact.
            supports_date_filter=False,
            # `search` already returns the full posting text: the detail call
            # it makes for the date carries jobDescription as well, so there is
            # nothing left for a second pass to fetch.
            supports_description_fetch=False,
            supports_pagination=True,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- Plumbing ----------------------------------------------------------

    def _client_for(self, tenant: WorkdayTenant) -> PortalHttpClient:
        host = tenant.host
        client = self._clients.get(host)
        if client is None:
            client = PortalHttpClient(portal=self.slug, policy=self._policy)
            self._clients[host] = client
        return client

    async def _robots_allows(self, tenant: WorkdayTenant) -> bool:
        """Whether this tenant's own policy permits its site and CXS path.

        Cached per origin for the life of the run. Both paths are checked,
        because tenants close individual sites while leaving the host open.
        """
        origin = origin_of(tenant.host)
        policy = self._robots.get(origin)

        if policy is None:
            client = self._client_for(tenant)
            try:
                body = await client.get_optional_text(robots_url(tenant.host))
            except (PortalBlockedError, PortalRateLimitedError):
                raise
            except PortalError as exc:
                # The host has a policy and would not give it to us. Declining
                # is the conservative reading and costs one employer.
                logger.info(
                    "workday_robots_unreadable",
                    portal=self.slug,
                    tenant=tenant.key,
                    error=exc.message,
                )
                policy = RobotsPolicy.unreadable_policy()
            else:
                policy = (
                    RobotsPolicy.permissive()
                    if body is None
                    else RobotsPolicy.parse(body, user_agent=self._policy.user_agent)
                )
            self._robots[origin] = policy

        allowed = policy.allows(tenant.jobs_url) and policy.allows(tenant.site_url)
        if not allowed:
            logger.info(
                "workday_tenant_robots_declined",
                portal=self.slug,
                tenant=tenant.key,
            )
        return allowed

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings matching the request's same-role title variations.

        Tenants are read concurrently and independently: one employer being
        unavailable costs that employer's listings, not the pass. Detail pages
        are opened only for titles that already match and are not already known
        to be stale, and only once each however many variations they answer.
        """
        if not self._tenants:
            raise PortalError("No Workday tenants are configured.", portal=self.slug)

        semaphore = asyncio.Semaphore(self._max_concurrent)
        # Tenants run concurrently, so each reports truncation by adding itself
        # here rather than by returning a flag the gather would flatten away.
        cut_short: set[str] = set()

        gathered = await asyncio.gather(
            *(
                self._collect_tenant(tenant, request, semaphore, cut_short)
                for tenant in self._tenants
            ),
            return_exceptions=True,
        )

        candidates: list[_Candidate] = []
        seen_urls: set[str] = set()
        failures: list[str] = []
        declined: list[str] = []

        for tenant, outcome in zip(self._tenants, gathered, strict=True):
            if isinstance(outcome, PortalBlockedError | PortalRateLimitedError):
                # A refusal is respected immediately and for the whole portal:
                # carrying on to the next tenant would be ignoring what was
                # just said, even though the next tenant is a different host.
                raise outcome
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, PortalError):
                    raise outcome
                logger.info(
                    "workday_tenant_unavailable",
                    portal=self.slug,
                    tenant=tenant.key,
                    error=outcome.message,
                )
                failures.append(tenant.key)
                continue
            if outcome is None:
                declined.append(tenant.key)
                continue

            for candidate in outcome:
                if candidate.url in seen_urls:
                    continue
                seen_urls.add(candidate.url)
                candidates.append(candidate)

        if declined and len(declined) == len(self._tenants):
            raise PortalBlockedError(
                f"Every configured Workday tenant declined by robots.txt "
                f"({len(declined)} checked).",
                portal=self.slug,
            )
        if failures and len(failures) + len(declined) == len(self._tenants):
            raise PortalError(
                f"No Workday tenant could be read ({len(failures)} attempted).",
                portal=self.slug,
            )

        # Trimming to the request's limit is the limit working, not the
        # deadline biting, so it is not truncation.
        jobs = await self._read_details(
            candidates[: max(request.limit, 0)], request, cut_short
        )

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            has_more=False,
            next_cursor=None,
            # Collected unfiltered; the caller applies the freshness window.
            window_applied_by_portal=False,
            # Any tenant that stopped early, at either stage. Rows and details
            # already read are cached for the life of this adapter, so the next
            # query resumes instead of starting over.
            partial=bool(cut_short),
        )

    async def _collect_tenant(
        self,
        tenant: WorkdayTenant,
        request: PortalSearchRequest,
        semaphore: asyncio.Semaphore,
        cut_short: set[str],
    ) -> list[_Candidate] | None:
        """Candidate rows from one tenant, or None when robots declined it.

        A tenant that is not reached before the deadline records itself in
        ``cut_short`` and returns nothing. It is not a failure and not a
        refusal: it is an employer this pass did not get to, and calling it
        either of the other two would be wrong.
        """
        async with semaphore:
            # Checked inside the semaphore, not outside it: the queue is where
            # the time goes, and a tenant admitted an instant before the
            # deadline should not then start a page of requests.
            if out_of_time(request):
                cut_short.add(tenant.key)
                return []

            if not await self._robots_allows(tenant):
                return None

            found: dict[str, _Candidate] = {}
            for query in request.queries:
                for row in await self._list_rows(tenant, query, request, cut_short):
                    title = str(row.get("title") or "").strip()
                    path = str(row.get("externalPath") or "").strip()
                    if not title or not path:
                        continue
                    if not any(title_matches(q, title) for q in request.queries):
                        continue

                    posted_on = row.get("postedOn")
                    posted_on = posted_on if isinstance(posted_on, str) else None
                    if is_stale_label(posted_on):
                        # Outside the window whatever its exact date, so it
                        # never costs a detail request.
                        continue

                    locations = row.get("locationsText")
                    found.setdefault(
                        path,
                        _Candidate(
                            tenant=tenant,
                            title=title,
                            external_path=path,
                            posted_on=posted_on,
                            locations_text=(
                                locations if isinstance(locations, str) else None
                            ),
                        ),
                    )

            return list(found.values())

    async def _list_rows(
        self,
        tenant: WorkdayTenant,
        query: str,
        request: PortalSearchRequest | None = None,
        cut_short: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Every row this tenant returns for one query, within the page bound.

        Paging stops on the reported ``total`` rather than on an empty page:
        an offset past the end returns rows again rather than nothing, so an
        empty-page test would never terminate.
        """
        rows: list[dict[str, Any]] = []

        for page in range(MAX_PAGES_PER_QUERY):
            offset = page * PAGE_SIZE
            cached = self._lists.get((tenant.key, query, offset))
            if cached is None:
                # A cached page costs nothing and is always taken; only a real
                # request is weighed against the clock. No request means no
                # deadline - the health check reads one page and is not part of
                # a run's budget.
                if request is not None and out_of_time(request):
                    if cut_short is not None:
                        cut_short.add(tenant.key)
                    break

                payload = await self._client_for(tenant).post_json(
                    tenant.jobs_url,
                    payload={"limit": PAGE_SIZE, "offset": offset, "searchText": query},
                )
                postings = payload.get("jobPostings")
                if not isinstance(postings, list):
                    raise PortalParseError(
                        f"Workday tenant {tenant.key} returned no jobPostings list.",
                        portal=self.slug,
                    )
                total = payload.get("total")
                cached = (
                    [item for item in postings if isinstance(item, dict)],
                    total if isinstance(total, int) and total >= 0 else 0,
                )
                self._lists[(tenant.key, query, offset)] = cached

            page_rows, total = cached
            rows.extend(page_rows)

            if not page_rows or offset + PAGE_SIZE >= total:
                break

        return rows

    async def _read_details(
        self,
        candidates: list[_Candidate],
        request: PortalSearchRequest,
        cut_short: set[str],
    ) -> list[RawJob]:
        """Open each candidate's detail page, once each, one host at a time.

        Grouped by host on purpose. ``_RateLimiter`` spaces *sequential* calls
        on one client; several coroutines awaiting it together all read the
        same last-request time, sleep the same interval and then fire at once,
        so firing a host's details concurrently would quietly defeat the very
        pacing this adapter claims to apply per host. Hosts still run in
        parallel - they are unrelated employers - but one host's requests are
        issued in order, which is what the configured rate actually describes.
        """
        if not candidates:
            return []

        by_host: dict[str, list[_Candidate]] = {}
        for candidate in candidates:
            by_host.setdefault(candidate.tenant.host, []).append(candidate)

        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def read_host(group: list[_Candidate]) -> list[RawJob | None]:
            async with semaphore:
                read: list[RawJob | None] = []
                for candidate in group:
                    # The expensive half of this adapter: one request per
                    # posting, issued in order for this host. Stopping here
                    # keeps every detail already read.
                    if out_of_time(request) and candidate.url not in self._details:
                        cut_short.add(candidate.tenant.key)
                        break
                    read.append(await self._read_detail(candidate))
                return read

        gathered = await asyncio.gather(
            *(read_host(group) for group in by_host.values()),
            return_exceptions=True,
        )

        jobs: list[RawJob] = []
        for outcome in gathered:
            if isinstance(outcome, PortalBlockedError | PortalRateLimitedError):
                raise outcome
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, PortalError):
                    raise outcome
                continue
            jobs.extend(job for job in outcome if job is not None)
        return jobs

    async def _read_detail(self, candidate: _Candidate) -> RawJob | None:
        """One posting's detail, which is where the date and the text live.

        Returns None when the page cannot be read or carries no usable
        publication date. A listing whose date is unknown could not pass the
        freshness gate anyway, and dropping it here keeps a dateless record out
        of the pipeline.
        """
        url = candidate.url
        if url in self._details:
            return self._details[url]

        try:
            payload = await self._client_for(candidate.tenant).get_json(url)
        except (PortalBlockedError, PortalRateLimitedError):
            raise
        except PortalError as exc:
            logger.info(
                "workday_job_unavailable",
                portal=self.slug,
                tenant=candidate.tenant.key,
                job=candidate.external_path,
                error=exc.message,
            )
            self._details[url] = None
            return None

        raw = self._to_raw_job(payload, candidate=candidate)
        self._details[url] = raw
        return raw

    def _to_raw_job(
        self, payload: dict[str, Any], *, candidate: _Candidate
    ) -> RawJob | None:
        """Map one Workday posting onto the shared RawJob contract."""
        posting = payload.get("jobPostingInfo")
        if not isinstance(posting, dict):
            logger.info(
                "workday_job_without_posting_info",
                portal=self.slug,
                tenant=candidate.tenant.key,
                job=candidate.external_path,
            )
            return None

        title = str(posting.get("title") or candidate.title or "").strip()
        if not title:
            return None

        posted_at = parse_start_date(posting.get("startDate"))
        if posted_at is None:
            logger.info(
                "workday_job_without_publication_date",
                portal=self.slug,
                tenant=candidate.tenant.key,
                job=candidate.external_path,
            )
            return None

        description = build_description(posting)
        if description is None:
            return None

        source_url = str(posting.get("externalUrl") or "").strip()
        if not source_url:
            # Workday builds the same URL from the site and the posting path;
            # falling back to it keeps a listing whose payload omitted the
            # field rather than dropping a real job over a missing string.
            source_url = (
                f"{candidate.tenant.host}/{candidate.tenant.site}"
                f"{candidate.external_path}"
            )

        req_id = posting.get("jobReqId")
        external_id = req_id.strip() if isinstance(req_id, str) and req_id.strip() else None

        entity = payload.get("hiringOrganization")
        entity_name = entity.get("name") if isinstance(entity, dict) else None

        return RawJob(
            external_id=external_id,
            source=self.slug,
            title=title,
            # Configured, never the payload's legal entity - see the module
            # docstring. This is also what stops one employer fragmenting into
            # several canonical keys.
            company=candidate.tenant.display_name,
            location=format_location(posting),
            description=description,
            source_url=source_url,
            application_url=f"{source_url.rstrip('/')}/apply",
            posted_at=posted_at,
            employment_type=employment_type(posting),
            raw_payload=declare_application_type(
                {
                    "tenant": candidate.tenant.as_dict(),
                    "postedOn": candidate.posted_on,
                    "locationsText": candidate.locations_text,
                    "jobPostingInfo": posting,
                    # Kept for debugging only. Never used as the employer.
                    "hiringOrganizationName": entity_name,
                },
                ApplicationType.LONG_APPLICATION,
            ),
        )

    # --- Contract ----------------------------------------------------------

    async def fetch_description(self, job: RawJob) -> str | None:
        """The full posting text already arrived with the listing."""
        return job.description

    async def validate_url(self, url: str) -> bool:
        tenant = next(
            (t for t in self._tenants if url.startswith(t.host)),
            self._tenants[0] if self._tenants else None,
        )
        if tenant is None:
            return False
        return await self._client_for(tenant).head_ok(url)

    async def health_check(self) -> PortalHealth:
        """Reachability of the first configured tenant's career site."""
        if not self._tenants:
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail="No Workday tenants are configured.",
            )

        tenant = self._tenants[0]
        try:
            if not await self._robots_allows(tenant):
                return PortalHealth(
                    is_available=False,
                    checked_at=datetime.now(UTC),
                    detail=f"robots.txt declines {tenant.key}.",
                )
            await self._list_rows(tenant, "")
        except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=f"Tenant did not respond: {type(exc).__name__}.",
            )
        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))


__all__ = [
    "DEFAULT_MAX_CONCURRENT_TENANTS",
    "DEFAULT_TENANTS",
    "MAX_PAGES_PER_QUERY",
    "PAGE_SIZE",
    "STALE_POSTED_MARKER",
    "US_COUNTRY_CODES",
    "US_COUNTRY_NAMES",
    "WorkdayPortal",
    "WorkdayTenant",
    "additional_locations",
    "build_description",
    "country_code",
    "country_name",
    "employment_type",
    "format_location",
    "html_to_text",
    "is_stale_label",
    "is_us_posting",
    "parse_start_date",
    "relative_label_days",
    "title_matches",
    "title_tokens",
    "workplace_label",
]
