"""SimplyHired adapter (https://www.simplyhired.com/).

Access surface, and why this one
--------------------------------
SimplyHired's ``robots.txt`` closes a specific list of paths to every client
and blocks a named set of crawlers outright. This client is not on that named
list, so the ``User-agent: *`` group applies::

    Disallow: /serp$        Disallow: /serp?        Disallow: /job-id/
    Disallow: /job-post/    Disallow: /c/jobs-api/  Disallow: /c/jobs-widget/
    Disallow: /out?r=       Disallow: /a/job-details/  ...

``/search`` and ``/job/`` are **not** on that list, and the site publishes
``/sitemap/viewjob/sitemap_index.xml`` itself. Those two are what this adapter
reads, and it reads nothing else. In particular:

* the private ``/c/jobs-api/`` surface is disallowed and is never called;
* ``/out?r=...``, the click-tracking redirect that ``encodedJobClickPingUrl``
  and ``encodedApplyUrl`` point at, is disallowed - so the listing's own
  ``botUrl`` is used as the link instead. That is the clean, stable, canonical
  URL the site offers to crawlers, and it is what a user opens.

Both surfaces answer this client's honest ``JobFinderBot`` user agent with a
200. Nothing here logs in, holds a session, or retries around a refusal.

What this portal gives
----------------------
* **A stable job identity.** ``jobKey`` is published per card and is the same
  value the job page is addressed by, so a re-search recognises the vacancy
  rather than duplicating it.
* **Real pagination.** ``pageCursors`` maps a page number to an opaque cursor,
  and page two returns twenty listings that do not overlap page one.
* **Structured requirements.** Each card carries a labelled ``requirements``
  list, plus job types, benefits, remote attributes and a salary string.

Descriptions, and the choice made here
--------------------------------------
The employer's full posting text exists, at ``/job/<jobKey>``, and
``fetch_description`` returns it. It is **not** fetched during ``search``:
that would be one request per listing, and with the collector's 30 second
per-query timeout a couple of dozen of them would time the portal out and lose
the whole pass. So ``search`` composes a description from the fields the
search page already returned - which are substantial - and the full text is
available on demand for a caller that wants it.

The composed text is labelled honestly. ``uncategorized`` is Indeed's own name
for phrases it extracted from the posting without classifying them; they are
included under a heading that says exactly that, rather than being presented
as requirements they were never stated to be.

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
    out_of_time,
)
from app.portals.exceptions import PortalParseError
from app.portals.http import HttpPolicy, PortalHttpClient

logger = get_logger(__name__)


def _positive_int(value: Any, default: int) -> int:
    """A configured positive integer, or the default.

    A missing, non-numeric or non-positive value falls back rather than
    disabling the bound: a misconfigured row must not create an endless search.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value > 0 else default

BASE_URL = "https://www.simplyhired.com"
SEARCH_PATH = f"{BASE_URL}/search"
JOB_PATH = f"{BASE_URL}/job"

#: The page data is embedded in this script tag by the site's framework.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

#: Twenty cards per page. Phase 12 searches deeply rather than to a quota, so
#: this is the *default* page count rather than a ceiling: raise the portal's
#: ``max_pages`` option to collect further without a deployment. The collector's
#: own request, runtime and job limits still bound the leg.
MAX_PAGES = 2

#: Below this the composed text is not worth returning: the matching layer
#: would reject it as unscoreable anyway, and a stub would look like a real
#: posting.
_MIN_USEFUL_DESCRIPTION = 80

#: Default when the caller names no location. The site requires one to search
#: nationally rather than guessing from an IP.
DEFAULT_LOCATION = "United States"


class SimplyHiredPortal(BasePortal):
    """Collects listings from SimplyHired's public search pages."""

    slug: ClassVar[str] = "simplyhired"
    display_name: ClassVar[str] = "SimplyHired"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug, policy=HttpPolicy.from_options(self.options)
        )
        self._max_pages = _positive_int(self.options.get("max_pages"), MAX_PAGES)

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # The search page carries no date filter this adapter can trust;
            # freshness is applied by the caller against posted_at, which
            # `dateOnIndeed` makes exact.
            supports_date_filter=False,
            # `/job/<jobKey>` carries the employer's full posting text.
            supports_description_fetch=True,
            supports_pagination=True,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings for the request's same-role title variations.

        Each query is issued independently, so one query returning nothing
        cannot lose the rest of the pass.
        """
        location = request.location or DEFAULT_LOCATION
        seen: set[str] = set()
        jobs: list[RawJob] = []
        truncated = False

        for query in request.queries:
            if len(jobs) >= request.limit:
                break

            cursor: str | None = None
            for _ in range(self._max_pages):
                if len(jobs) >= request.limit:
                    break

                if out_of_time(request):
                    # The caller is out of time. Returning what is already here
                    # keeps it; being cancelled mid-loop would not.
                    truncated = True
                    break

                page = await self._search_once(
                    query,
                    location=location,
                    cursor=cursor,
                    remote_only=request.remote_only,
                )
                records = page["jobs"]
                if not records:
                    break

                for record in records:
                    raw = self._to_raw_job(record)
                    if raw is None:
                        continue
                    identity = raw.external_id or raw.source_url
                    if identity in seen:
                        continue
                    seen.add(identity)
                    jobs.append(raw)
                    if len(jobs) >= request.limit:
                        break

                cursor = self._next_cursor(page)
                if cursor is None:
                    break

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            has_more=False,
            next_cursor=None,
            # Collected unfiltered; the caller applies the window against
            # posted_at.
            window_applied_by_portal=False,
            # True only when a deadline stopped the sweep early. The jobs
            # above are complete and valid; the sweep is not.
            partial=truncated,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        """The employer's full posting text, from the listing's own page.

        Returns None rather than raising when the page no longer carries the
        job: a listing that has since expired is not an adapter failure, and
        the composed description collected earlier still stands.
        """
        if not job.external_id:
            return job.description

        try:
            payload = await self._page_props(f"{JOB_PATH}/{job.external_id}")
        except PortalParseError:
            logger.info(
                "simplyhired_description_unavailable",
                portal=self.slug,
                external_id=job.external_id,
            )
            return job.description

        if payload.get("jobFound") is False or payload.get("expired") is True:
            return job.description

        text = html_to_text(payload.get("jobDescriptionHtml"))
        return text or job.description

    async def validate_url(self, url: str) -> bool:
        return await self._http.head_ok(url)

    async def health_check(self) -> PortalHealth:
        reachable = await self._http.head_ok(BASE_URL)
        return PortalHealth(
            is_available=reachable,
            checked_at=datetime.now(UTC),
            detail=None if reachable else "Search page did not respond.",
        )

    # --- Fetching ----------------------------------------------------------

    async def _page_props(
        self, url: str, *, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fetch a page and return its embedded ``pageProps``."""
        body = await self._http.get_text(url, params=params)

        match = _NEXT_DATA_RE.search(body)
        if match is None:
            raise PortalParseError(
                "SimplyHired page did not contain the expected embedded data.",
                portal=self.slug,
            )
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise PortalParseError(
                f"SimplyHired embedded data was not valid JSON: {exc.msg}.",
                portal=self.slug,
            ) from exc

        props = payload.get("props", {}).get("pageProps")
        if not isinstance(props, dict):
            raise PortalParseError(
                "SimplyHired page data contained no pageProps.", portal=self.slug
            )
        return props

    async def _search_once(
        self,
        query: str,
        *,
        location: str,
        cursor: str | None,
        remote_only: bool,
    ) -> dict[str, Any]:
        """One search page: its listings and its pagination cursors."""
        params: dict[str, Any] = {"q": query, "l": location}
        if cursor:
            params["cursor"] = cursor
        if remote_only:
            params["rl"] = "Remote"

        props = await self._page_props(SEARCH_PATH, params=params)

        records = props.get("jobs")
        if records is None:
            # A query with no matches is legitimate; a page with no jobs key at
            # all means the shape changed and should be surfaced.
            raise PortalParseError(
                "SimplyHired search page contained no jobs key.", portal=self.slug
            )
        if not isinstance(records, list):
            raise PortalParseError(
                "SimplyHired search results were not a list.", portal=self.slug
            )

        return {
            "jobs": [record for record in records if isinstance(record, dict)],
            "pageCursors": props.get("pageCursors"),
            "currentPageNumber": props.get("currentPageNumber"),
        }

    @staticmethod
    def _next_cursor(page: dict[str, Any]) -> str | None:
        """The cursor for the page after this one, when there is one."""
        cursors = page.get("pageCursors")
        if not isinstance(cursors, dict):
            return None

        current = page.get("currentPageNumber")
        if not isinstance(current, int):
            return None

        value = cursors.get(str(current + 1))
        return value if isinstance(value, str) and value else None

    # --- Parsing -----------------------------------------------------------

    def _to_raw_job(self, record: dict[str, Any]) -> RawJob | None:
        """Map one SimplyHired card onto the shared RawJob contract.

        Returns None when the card lacks the minimum needed to be usable - a
        key and a title. A listing the user cannot open is not a result.
        """
        job_key = str(record.get("jobKey") or "").strip()
        title = str(record.get("title") or "").strip()

        if not job_key or not title:
            logger.debug(
                "simplyhired_record_skipped",
                portal=self.slug,
                has_key=bool(job_key),
                has_title=bool(title),
            )
            return None

        # The crawler-facing URL, free of the per-card tracking parameters that
        # `encodedUrl` carries. `/out?r=` is disallowed and is never used.
        url = f"{JOB_PATH}/{job_key}"

        return RawJob(
            external_id=job_key,
            source=self.slug,
            title=title,
            company=str(record.get("company") or "").strip() or None,
            location=str(record.get("location") or "").strip() or None,
            description=self._description(record),
            source_url=url,
            application_url=url,
            posted_at=self._posted_at(record.get("dateOnIndeed")),
            employment_type=self._employment_type(record),
            raw_payload=declare_application_type(
                dict(record), self._application_type(record)
            ),
        )

    @staticmethod
    def _application_type(record: dict[str, Any]) -> ApplicationType:
        """What the card says about how the user will apply (Phase 13).

        SimplyHired is an Indeed property and publishes ``indeedApply`` on every
        card. ``True`` means Indeed's own one-click submission against a stored
        profile - the user never reaches the employer's form. That is precisely
        what this product refuses, and it is the majority of what this portal
        returns: 44 of 60 cards in the captured fixture.

        ``False`` is an equally real statement - the card links out to the
        employer's own application - so it is reported as a long application
        rather than left unknown.

        A card missing the field entirely is left ``UNKNOWN``. Reading absence
        as either answer would be guessing, and the shared classifier still
        examines the listing for evidence afterwards.
        """
        flag = record.get("indeedApply")
        if flag is True:
            return ApplicationType.QUICK_APPLY
        if flag is False:
            return ApplicationType.LONG_APPLICATION
        return ApplicationType.UNKNOWN

    @classmethod
    def _description(cls, record: dict[str, Any]) -> str | None:
        """Compose a description from what the search page published.

        Sections are labelled so the result reads as the summary it is, and so
        the matching layer's requirement extraction has the headings it looks
        for. ``uncategorized`` is Indeed's own label for phrases pulled out of
        the posting without being classified; it is presented as exactly that
        and not as stated requirements.
        """
        sections: list[str] = []

        snippet = cls._clean(record.get("snippet"))
        if snippet:
            sections.append(snippet)

        requirements = cls._bullets(record.get("requirements"))
        if requirements:
            sections.append(f"Requirements:\n{requirements}")

        for label, key in (
            ("Job type", "jobTypes"),
            ("Work arrangement", "remoteAttributes"),
        ):
            joined = cls._joined(record.get(key))
            if joined:
                sections.append(f"{label}: {joined}")

        salary = cls._clean(record.get("salaryInfo"))
        if salary:
            sections.append(f"Compensation: {salary}")

        benefits = cls._joined(record.get("benefits"))
        if benefits:
            sections.append(f"Benefits: {benefits}")

        extra = cls._bullets(record.get("uncategorized"))
        if extra:
            sections.append(f"Also mentioned in this posting:\n{extra}")

        composed = "\n\n".join(sections)
        return composed if len(composed) >= _MIN_USEFUL_DESCRIPTION else None

    @staticmethod
    def _clean(value: Any) -> str | None:
        """Unescape and tidy one of the site's short text fields."""
        if not isinstance(value, str):
            return None
        text = html.unescape(_TAG_RE.sub(" ", value))
        text = re.sub(r"[ \t]+", " ", text).strip()
        return text or None

    @classmethod
    def _bullets(cls, values: Any) -> str | None:
        if not isinstance(values, list):
            return None
        items = [cleaned for item in values if (cleaned := cls._clean(item))]
        return "\n".join(f"- {item}" for item in items) if items else None

    @classmethod
    def _joined(cls, values: Any) -> str | None:
        if isinstance(values, str):
            return cls._clean(values)
        if not isinstance(values, list):
            return None
        items = [cleaned for item in values if (cleaned := cls._clean(item))]
        return ", ".join(items) if items else None

    @staticmethod
    def _employment_type(record: dict[str, Any]) -> str | None:
        """First stated job type, e.g. "Full-time"."""
        types = record.get("jobTypes")
        if isinstance(types, list) and types:
            first = str(types[0]).strip()
            return first or None
        if isinstance(types, str) and types.strip():
            return types.strip()
        return None

    @staticmethod
    def _posted_at(value: Any) -> datetime | None:
        """Convert the site's epoch-milliseconds timestamp.

        A listing without one keeps ``posted_at`` empty rather than being given
        a made-up date.
        """
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if value <= 0:
            return None
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None


def html_to_text(value: Any) -> str | None:
    """Flatten a posting's HTML body into readable plain text.

    Block boundaries become newlines so headings and list items do not run
    together, because the matching layer reads those headings to tell a stated
    requirement from prose.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</(p|div|li|ul|ol|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RUN_RE.sub("\n\n", text).strip()

    return text or None


__all__ = [
    "BASE_URL",
    "DEFAULT_LOCATION",
    "JOB_PATH",
    "MAX_PAGES",
    "SEARCH_PATH",
    "SimplyHiredPortal",
    "html_to_text",
]
