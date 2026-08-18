"""Google Careers adapter (https://www.google.com/about/careers/).

Access surface, and how it was found
------------------------------------
Google Careers renders its results **server-side**. The search page embeds its
own data in the page it returns, in the ``AF_initDataCallback`` blocks Google
uses across its properties::

    <script class="ds:1">AF_initDataCallback({key: 'ds:1', hash: '2',
      data:[[[ "121773752340882118", "Strategy and Operations Senior Associate", ...
      ]], null, 99, 20], sideChannel: {}});</script>

``data`` is ordinary JSON. So there is no private API to call and nothing to
reverse-engineer: the listings arrive with the robots-allowed page itself, in
one request, already structured.

Nothing here logs in, holds a session, solves a challenge, or disguises the
client. The request goes out under this project's own ``JobFinderBot`` user
agent - **verified to be served identically to a browser's**, so the honest
identification costs nothing and is not worked around.

What robots.txt allows, and the limit it sets
---------------------------------------------
``https://www.google.com/robots.txt`` permits the results page and the
individual job pages for ``User-agent: *``. It disallows **pagination**, four
ways::

    Disallow: /about/careers/applications/jobs/results?page=
    Disallow: /about/careers/applications/jobs/results/?page=
    Disallow: /about/careers/applications/jobs/results?*&page=
    Disallow: /about/careers/applications/jobs/results/?*&page=

(The blanket ``Disallow: .../jobs/results`` further down the file belongs to
the ``User-agent: Yandex`` group, not to ``*``.)

So this adapter reads **page one only**, which is 20 listings per query, and
that is a deliberate stop rather than a missing feature. Google's own page-two
request can be reached by other means; using them would achieve exactly what
these four rules forbid, so it is not done. Breadth comes from querying once
per same-role title variation - the same shape already used by Hiring Cafe,
whose ``?page=`` is disallowed in the same way, and by Jobright and Dayforce,
which cannot paginate at all.

Which timestamp is the posting date
-----------------------------------
This is the one field that needed proving rather than reading, because the
payload carries **three** timestamps and the obvious one is wrong.

Sorting with ``sort_by=date`` and comparing the three across a live page shows
that the site's own "newest" ordering follows index 13, not index 12 - and that
index 13 is a *refresh* time. Three of twenty listings had been refreshed long
after they were published::

    Staff Software Engineer, AI/ML   published 2026-07-13   refreshed 2026-08-18
    Strategy Associate, YouTube      published 2026-07-31   refreshed 2026-08-17
    Global Solutions Senior Assoc.   published 2026-01-28   refreshed 2026-08-17

Index 12 is the publication time; index 13 is when the record was last
modified; index 14 trails 13 by microseconds and is an internal write time.

``posted_at`` therefore comes from **index 12 and only index 12**. Taking the
site's own sort key would have dated that last listing to "today" when the
employer published it seven months ago, which is precisely the
crawl-date-as-posting-date substitution CLAUDE.md section 6 forbids. The
consequence is visible and intended: listings Google still shows near the top
of "most recent" are excluded here by the 30-day window, because they are old.

A record with no usable publication timestamp keeps ``posted_at`` empty and is
excluded downstream, rather than being given a date.

The honest limitations
----------------------
* **One employer.** This is Google's own careers site, so coverage is Google
  and the organisations that hire through it - DeepMind, YouTube, Waymo, Fitbit
  and the rest of the tenants the page itself names. It is not a job board.
* **20 listings per query**, for the robots reason above.
* **The query is semantic, not literal.** Asking for "Data analyst" returns
  strategy and engineering roles too. That is the site's behaviour; the title
  filter here narrows it and the role gate downstream is the authority.

What it gives in exchange
-------------------------
* **The employer's complete posting text** - about, responsibilities and
  qualifications - inline, with no second request.
* **A real publication timestamp**, as established above.
* **Structured locations** carrying ISO country codes, so the US rule is
  applied to published data rather than to a parsed string.
* **A stable job id and a stable job URL**: the numeric id alone resolves, so
  the URL is built from published data with no slug invented.

This adapter collects and normalises. It does not score, deduplicate, rank or
apply to anything.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

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
from app.portals.exceptions import PortalError, PortalParseError
from app.portals.http import HttpPolicy, PortalHttpClient

logger = get_logger(__name__)

SITE_BASE = "https://www.google.com/about/careers/applications"
RESULTS_URL = f"{SITE_BASE}/jobs/results"

#: The location filter sent with every query. Google applies it server-side,
#: returning listings that have at least one location in the named country.
#: Overridable through ``portal_configs.options["location"]``.
DEFAULT_LOCATION = "United States"

#: Google's server-rendered data island holding the search results.
#:
#: The value is captured without assuming it is a list, so a block that is
#: present but malformed reaches the shape checks in ``parse_results`` and is
#: reported as a parse error. Requiring ``[...]`` here would instead make a
#: changed layout look like a page with no results, which is the one thing this
#: must not do.
_DS1_RE = re.compile(
    r"AF_initDataCallback\(\{key:\s*'ds:1'.*?data:(.*?),\s*sideChannel",
    re.S,
)

# --- Field positions in one result record -----------------------------------
#
# The payload is a positional array, so these names are the schema. They were
# read off a live response and are asserted by the fixture tests, which is what
# turns a silent layout change into a failing test rather than wrong data.
_ID = 0
_TITLE = 1
_RESPONSIBILITIES = 3
_QUALIFICATIONS = 4
_COMPANY = 7
_LOCATIONS = 9
_ABOUT = 10
#: Publication time. NOT 13 (last modified) or 14 (internal write time) - see
#: the module docstring; this distinction is the whole reason it is named here.
_PUBLISHED = 12

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[a-z0-9]+")

#: Single letters match nearly every title and carry no signal.
_MIN_TOKEN_LENGTH = 2

#: Positions inside one entry of the locations array.
_LOC_FORMATTED = 0
_LOC_CITY = 2
_LOC_REGION = 4
_LOC_COUNTRY = 5


def strip_html(value: str | None) -> str:
    """Readable text from Google's posting HTML.

    List markup becomes newlines so the scorer sees the qualifications as
    separate lines rather than one run-on sentence.
    """
    if not value:
        return ""
    text = re.sub(r"<li[^>]*>", "\n- ", value, flags=re.I)
    text = re.sub(r"</(p|div|h[1-6]|ul|ol|tr)>", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


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
    """Whether a returned title actually answers the query.

    Google's search is semantic: "Data analyst" comes back with "Novel AI Lead
    Methodologist" and "Strategy and Operations Senior Associate". That is the
    site's behaviour, not a defect, but it makes the response a candidate list
    rather than an answer.

    Every significant word of the query must appear in the title, so "Financial
    Analyst" reaches "Senior Financial Analyst" but not "Business Analyst".
    Deliberately blunt: the role gate downstream is the authority on relevance
    (CLAUDE.md section 7).
    """
    wanted = title_tokens(query)
    return bool(wanted) and wanted <= title_tokens(title)


def _text_field(record: list[Any], index: int) -> str:
    """One of the ``[null, "<html>"]`` text pairs Google uses for prose."""
    if index >= len(record):
        return ""
    value = record[index]
    if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
        return value[1]
    return value if isinstance(value, str) else ""


def build_description(record: list[Any]) -> str | None:
    """The employer's complete posting text, in the order the page shows it."""
    sections = (
        ("", _text_field(record, _ABOUT)),
        ("Responsibilities", _text_field(record, _RESPONSIBILITIES)),
        ("Qualifications", _text_field(record, _QUALIFICATIONS)),
    )
    parts: list[str] = []
    for heading, raw in sections:
        body = strip_html(raw)
        if not body:
            continue
        parts.append(f"{heading}\n{body}" if heading else body)

    joined = "\n\n".join(parts).strip()
    return joined or None


def format_location(record: list[Any]) -> str | None:
    """The posting's location as Google published it.

    Prefers a United States entry when a posting lists several, because this
    product returns US vacancies only and the US location is the one that
    matters. Google's own filter returns any posting with *at least one*
    location in the requested country, so a US role that also hires in Zurich
    arrives with both - picking the US entry describes it accurately rather
    than letting an incidental second office decide.

    The verdict itself is still made downstream by ``app.processing`` against
    this string; nothing here decides eligibility.
    """
    if _LOCATIONS >= len(record):
        return None
    locations = record[_LOCATIONS]
    if not isinstance(locations, list):
        return None

    entries = [entry for entry in locations if isinstance(entry, list) and entry]
    if not entries:
        return None

    def country(entry: list[Any]) -> str | None:
        if len(entry) > _LOC_COUNTRY and isinstance(entry[_LOC_COUNTRY], str):
            return entry[_LOC_COUNTRY]
        return None

    preferred = next((e for e in entries if country(e) == "US"), entries[0])

    formatted = preferred[_LOC_FORMATTED]
    if isinstance(formatted, str) and formatted.strip():
        return formatted.strip()

    # Build from the parts when no pre-formatted address was published, rather
    # than dropping an otherwise usable listing.
    parts = [
        preferred[index]
        for index in (_LOC_CITY, _LOC_REGION, _LOC_COUNTRY)
        if len(preferred) > index and isinstance(preferred[index], str)
    ]
    joined = ", ".join(part.strip() for part in parts if part.strip())
    return joined or None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse Google's ``[seconds, nanos]`` publication timestamp.

    A record without a usable one keeps ``posted_at`` empty rather than being
    given a made-up date; the freshness gate then excludes it, which is the
    behaviour CLAUDE.md section 6 requires for an unverifiable date.
    """
    if not isinstance(value, list) or not value:
        return None
    seconds = value[0]
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_results(body: str) -> list[list[Any]]:
    """Extract the result records from a served Google Careers page.

    A page with no data island is an **empty result**, not a failure: Google
    serves exactly that for a query nothing matches, and reporting it as a
    parse error would turn "no jobs today" into a portal outage.

    A data island that is present but undecodable *is* a failure, because that
    means the layout changed and the caller must be told rather than shown an
    empty list.
    """
    match = _DS1_RE.search(body)
    if match is None:
        return []

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise PortalParseError(
            f"Google Careers served a results block that did not decode: {exc.msg}.",
            portal=GoogleCareersPortal.slug,
        ) from exc

    if not isinstance(payload, list) or not payload:
        raise PortalParseError(
            "Google Careers served a results block of an unexpected shape.",
            portal=GoogleCareersPortal.slug,
        )

    records = payload[0]
    if records is None:
        return []
    if not isinstance(records, list):
        raise PortalParseError(
            "Google Careers served a results list of an unexpected shape.",
            portal=GoogleCareersPortal.slug,
        )
    return [record for record in records if isinstance(record, list)]


class GoogleCareersPortal(BasePortal):
    """Collects listings from Google's own server-rendered careers search."""

    slug: ClassVar[str] = "google_careers"
    display_name: ClassVar[str] = "Google Careers"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug, policy=HttpPolicy.from_options(self.options)
        )
        location = self.options.get("location", DEFAULT_LOCATION)
        self._location = location if isinstance(location, str) and location else None

    @property
    def location(self) -> str | None:
        """The country filter sent to Google, as configured."""
        return self._location

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # sort_by=date orders by the *refresh* time, not the publication
            # time, so it cannot be trusted as a freshness filter. Freshness is
            # applied by the caller against posted_at, which index 12 makes
            # exact. See the module docstring.
            supports_date_filter=False,
            # The full posting text arrives with the listing.
            supports_description_fetch=False,
            # Not absent - disallowed. robots.txt closes ?page= four ways, so
            # page one is all this adapter reads.
            supports_pagination=False,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings for the request's same-role title variations.

        Queries are independent: one query failing costs that variation's
        listings, not the whole pass. Every query failing is the portal being
        unavailable, and is raised so the run records it.
        """
        seen: set[str] = set()
        jobs: list[RawJob] = []
        failures = 0

        for query in request.queries:
            if len(jobs) >= request.limit:
                break

            try:
                records = await self._search_query(query)
            except PortalParseError:
                # The layout changed. That is not one query's problem, and
                # silently returning fewer jobs would hide it.
                raise
            except PortalError as exc:
                logger.info(
                    "google_careers_query_failed",
                    portal=self.slug,
                    query=query,
                    error=exc.message,
                )
                failures += 1
                continue

            for record in records:
                if len(jobs) >= request.limit:
                    break
                if not title_matches(query, self._title_of(record)):
                    continue

                raw = self._to_raw_job(record)
                if raw is None:
                    continue
                identity = raw.external_id or raw.source_url
                if identity in seen:
                    continue
                seen.add(identity)
                jobs.append(raw)

        if failures and failures == len(request.queries):
            raise PortalError(
                f"No Google Careers query could be read ({failures} attempted).",
                portal=self.slug,
            )

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            # Page one is all robots.txt permits, so there is no further page
            # to ask for. Claiming otherwise would make the collector request
            # a URL this adapter must not fetch.
            has_more=False,
            next_cursor=None,
            # Collected unfiltered; the caller applies the freshness window.
            window_applied_by_portal=False,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        """The full posting text already arrived with the listing."""
        return job.description

    async def validate_url(self, url: str) -> bool:
        return await self._http.head_ok(url)

    async def health_check(self) -> PortalHealth:
        """Reachability of the search page."""
        try:
            await self._search_query("analyst")
        except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=f"Careers search did not respond: {type(exc).__name__}.",
            )
        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))

    # --- Fetching ----------------------------------------------------------

    async def _search_query(self, query: str) -> list[list[Any]]:
        """One page-one request for one title variation."""
        params: dict[str, str] = {"q": query, "sort_by": "date"}
        if self._location:
            params["location"] = self._location

        body = await self._http.get_text(RESULTS_URL, params=params)
        return parse_results(body)

    # --- Mapping -----------------------------------------------------------

    @staticmethod
    def _title_of(record: list[Any]) -> str:
        if _TITLE >= len(record) or not isinstance(record[_TITLE], str):
            return ""
        return record[_TITLE].strip()

    def _to_raw_job(self, record: list[Any]) -> RawJob | None:
        """Map one Google Careers record onto the shared RawJob contract.

        Returns None when the record lacks the minimum needed to be usable - an
        id and a title. A listing the user cannot open is not a result.
        """
        job_id = record[_ID] if _ID < len(record) else None
        title = self._title_of(record)

        if not isinstance(job_id, (str, int)) or str(job_id).strip() in ("", "None"):
            return None
        if not title:
            return None

        job_id = str(job_id).strip()
        # The numeric id alone resolves to the posting - verified against the
        # live site - so no title slug is invented to complete the URL.
        url = f"{RESULTS_URL}/{job_id}"

        company = record[_COMPANY] if _COMPANY < len(record) else None
        company = company.strip() if isinstance(company, str) and company.strip() else None

        return RawJob(
            external_id=job_id,
            source=self.slug,
            title=title,
            company=company,
            location=format_location(record),
            description=build_description(record),
            source_url=url,
            application_url=url,
            # Index 12 only. See the module docstring.
            posted_at=parse_timestamp(
                record[_PUBLISHED] if _PUBLISHED < len(record) else None
            ),
            # Google publishes no employment-type field on this payload.
            employment_type=None,
            # Google Careers is not a board linking out to an employer's
            # application - it *is* the employer's application system, and the
            # posting page is where the form lives. A statement about the
            # surface, not a guess about one listing.
            raw_payload=declare_application_type(
                {"record": record}, ApplicationType.LONG_APPLICATION
            ),
        )


__all__ = [
    "DEFAULT_LOCATION",
    "RESULTS_URL",
    "SITE_BASE",
    "GoogleCareersPortal",
    "build_description",
    "format_location",
    "parse_results",
    "parse_timestamp",
    "strip_html",
    "title_matches",
    "title_tokens",
]
