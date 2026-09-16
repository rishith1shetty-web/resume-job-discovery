"""Jobright adapter (https://jobright.ai/).

Access surface, and why this one
--------------------------------
Jobright's ``robots.txt`` allows ``/jobs/*`` and ``/role/*`` and explicitly
disallows ``/api/*``. This adapter therefore reads only the server-rendered
taxonomy pages at ``https://jobright.ai/jobs/<slug>``, which embed their job
data in the page's ``__NEXT_DATA__`` payload. The private JSON API is never
called.

Known limitations, stated up front
----------------------------------
* **No pagination.** ``?page=2`` and ``?p=2`` return the first page again, so
  one taxonomy page yields roughly 16 listings regardless of how many exist.
  Breadth comes from querying several related taxonomy slugs, not from paging.
* **Location variants mostly repeat themselves.** The taxonomy publishes
  ``<role>-in-<city>-<state>`` pages, but measured live they serve one shared
  list: ``financial-analyst-in-chicago-il``, ``-in-austin-tx`` and
  ``-in-san-francisco-ca`` returned the *same* sixteen records as
  ``-in-new-york-ny``, carrying locations like "United States". So exactly one
  location variant is read. Iterating all twenty-four would cost twenty-two
  extra requests for zero extra listings, which is why this adapter does not.
* **Partial role coverage.** The taxonomy has ~4,500 slugs and is strong on
  technology, finance and analytics, but does not cover every occupation. A
  role with no matching slug returns nothing rather than a wrong match.
* **Descriptions are Jobright's summaries**, composed from the summary,
  responsibilities and requirements fields it publishes — not the employer's
  verbatim posting, which lives behind the disallowed API.
* **No employer-direct URL.** The listing exposes a Jobright job page, which is
  the usable link a user opens to apply. Resolving through to the employer's
  own form is Phase 5's problem, not this adapter's.

This adapter collects and normalises. It does not score, deduplicate, rank or
apply to anything.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.core.constants import FreshnessWindow
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
from app.portals.exceptions import PortalError, PortalParseError
from app.portals.http import HttpPolicy, PortalHttpClient
from app.processing.us_locations import is_us_location_slug

logger = get_logger(__name__)

BASE_URL = "https://jobright.ai"
TAXONOMY_SITEMAP = f"{BASE_URL}/sitemap-taxonomy.xml"

#: Taxonomy pages read for one role title: the base slug plus a single location
#: variant. Two rather than ten because Jobright's location pages repeat one
#: shared list - see the module docstring. Override with
#: ``portal_configs.options["max_slugs"]``.
MAX_SLUGS_PER_ROLE = 2

#: Interchangeable words in a job title. Jobright's taxonomy carries
#: ``corporate-finance-analyst`` but not ``corporate-financial-analyst``, and
#: without this the left-trim fallback below would silently drop "Corporate"
#: and search the plain base role instead - losing sixteen distinct listings
#: that the more specific page does carry.
#:
#: Only morphological variants of the *same* word belong here. This is not a
#: synonym list and must never be used to reach an adjacent occupation.
_WORD_VARIANTS: dict[str, tuple[str, ...]] = {
    "financial": ("finance",),
    "finance": ("financial",),
}

#: The page data is embedded in this script tag by the site's framework.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")

#: "2026-08-03 14:52:29" — no timezone marker; Jobright publishes UTC.
_PUBLISH_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def slugify_role(role: str) -> str:
    """Turn a role title into Jobright's taxonomy slug form.

    ``"Financial Analyst"`` -> ``"financial-analyst"``.
    """
    lowered = role.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-")


def _wording_variants(candidate: str) -> tuple[str, ...]:
    """The same title slug spelled the other way, one word at a time.

    ``"corporate-financial-analyst"`` -> ``("corporate-finance-analyst",)``.
    Word order and every other word are preserved, so the result still names
    the same role.
    """
    parts = candidate.split("-")
    variants: list[str] = []
    for index, word in enumerate(parts):
        for replacement in _WORD_VARIANTS.get(word, ()):
            swapped = [*parts]
            swapped[index] = replacement
            variants.append("-".join(swapped))
    return tuple(variants)


class JobrightPortal(BasePortal):
    """Collects listings from Jobright's public taxonomy pages."""

    slug: ClassVar[str] = "jobright"
    display_name: ClassVar[str] = "Jobright"

    #: Filled from the sitemap on first use so role -> slug matching is driven
    #: by Jobright's own taxonomy rather than a list hardcoded here.
    _taxonomy_cache: ClassVar[frozenset[str] | None] = None

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug, policy=HttpPolicy.from_options(self.options)
        )
        configured = self.options.get("max_slugs")
        self._max_slugs = (
            configured
            if isinstance(configured, int)
            and not isinstance(configured, bool)
            and configured > 0
            else MAX_SLUGS_PER_ROLE
        )
        #: Slug -> records, for this adapter's lifetime, which is one run. The
        #: run asks for each same-role title variation separately and several
        #: of them resolve to the same page.
        self._pages: dict[str, list[dict[str, Any]]] = {}

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # The taxonomy pages carry no date filter; freshness is applied by
            # the caller against posted_at.
            supports_date_filter=False,
            # Summaries arrive with the listing, so no second fetch is needed.
            supports_description_fetch=False,
            # `?page=` is ignored by the server-rendered surface.
            supports_pagination=False,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings for the request's same-role title variations.

        Each query is matched to a taxonomy slug and fetched independently, so
        one bad slug cannot lose the whole pass.
        """
        if out_of_time(request):
            # The taxonomy is a request of its own, and it is the first thing
            # this adapter does. Loading it with no time left to use it would
            # spend the one request the deadline was meant to save.
            return PortalSearchResult(
                jobs=[], window=request.window, partial=True
            )

        taxonomy = await self._load_taxonomy()
        seen: set[str] = set()
        jobs: list[RawJob] = []
        attempted = 0
        failure: PortalError | None = None
        truncated = False

        for query in request.queries:
            if len(jobs) >= request.limit:
                break

            slugs = self._match_slugs(query, taxonomy, max_slugs=self._max_slugs)
            if not slugs:
                logger.info(
                    "jobright_no_taxonomy_match", portal=self.slug, query=query
                )
                continue

            for slug in slugs:
                if len(jobs) >= request.limit:
                    break

                if out_of_time(request):
                    # The caller is out of time. Returning what is already here
                    # keeps it; being cancelled mid-loop would not.
                    truncated = True
                    break

                attempted += 1
                try:
                    records = await self._records_for(slug)
                except PortalError as exc:
                    # A secondary page failing costs that page's listings, not
                    # what the earlier slugs already produced.
                    failure = exc
                    logger.info(
                        "jobright_slug_unavailable",
                        portal=self.slug,
                        slug=slug,
                        error=exc.message,
                    )
                    continue

                for record in records:
                    raw = self._to_raw_job(record)
                    if raw is None or raw.external_id in seen:
                        continue
                    seen.add(raw.external_id or raw.source_url)
                    jobs.append(raw)
                    if len(jobs) >= request.limit:
                        break

        if failure is not None and not jobs and attempted:
            # Nothing collected and every page attempted failed: the portal is
            # unavailable, and saying "no matching jobs" would be untrue.
            raise failure

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            # The surface does not page, so there is never more to fetch.
            has_more=False,
            next_cursor=None,
            window_applied_by_portal=False,
            # True only when a deadline stopped the sweep early. The jobs
            # above are complete and valid; the sweep is not.
            partial=truncated,
        )

    async def _records_for(self, slug: str) -> list[dict[str, Any]]:
        """One taxonomy page's listings, fetched at most once per run.

        The URL carries no query string, so no paging parameter can be added
        here - ``?page=`` is ignored by this surface anyway.
        """
        cached = self._pages.get(slug)
        if cached is not None:
            return cached

        html = await self._http.get_text(f"{BASE_URL}/jobs/{slug}")
        records = self._extract_records(html)
        self._pages[slug] = records
        return records

    async def fetch_description(self, job: RawJob) -> str | None:
        """Descriptions already arrive with the listing.

        The detail page renders its body from the disallowed API, so there is
        nothing further to fetch without crossing that line.
        """
        return job.description

    async def validate_url(self, url: str) -> bool:
        return await self._http.head_ok(url)

    async def health_check(self) -> PortalHealth:
        reachable = await self._http.head_ok(f"{BASE_URL}/jobs/backend-engineer")
        return PortalHealth(
            is_available=reachable,
            checked_at=datetime.now(UTC),
            detail=None if reachable else "Taxonomy page did not respond.",
        )

    # --- Taxonomy ----------------------------------------------------------

    async def _load_taxonomy(self) -> frozenset[str]:
        """Fetch and cache Jobright's taxonomy slugs from its sitemap."""
        if type(self)._taxonomy_cache is not None:
            return type(self)._taxonomy_cache

        xml = await self._http.get_text(TAXONOMY_SITEMAP)
        slugs = {
            url.rsplit("/", 1)[-1]
            for url in _SITEMAP_LOC_RE.findall(xml)
            if "/jobs/" in url
        }
        if not slugs:
            raise PortalParseError(
                "Jobright taxonomy sitemap contained no job URLs.", portal=self.slug
            )

        type(self)._taxonomy_cache = frozenset(slugs)
        logger.info("jobright_taxonomy_loaded", portal=self.slug, count=len(slugs))
        return type(self)._taxonomy_cache

    @staticmethod
    def _match_slug(query: str, taxonomy: frozenset[str]) -> str | None:
        """Best taxonomy slug for a role title, or None.

        Exact match first; then the longest slug that is a prefix-word match.
        Returning None is correct when nothing fits — a wrong slug would mean
        searching a different role, which the product forbids.
        """
        candidate = slugify_role(query)
        if not candidate:
            return None
        if candidate in taxonomy:
            return candidate

        # A wording variant of the whole title is tried before any word is
        # dropped: "corporate-financial-analyst" should reach
        # "corporate-finance-analyst" rather than fall back to the plainer
        # "financial-analyst", which is a less specific search.
        for alternative in _wording_variants(candidate):
            if alternative in taxonomy:
                return alternative

        # e.g. "senior-financial-analyst" -> "financial-analyst"
        parts = candidate.split("-")
        for start in range(len(parts)):
            trimmed = "-".join(parts[start:])
            if trimmed in taxonomy:
                return trimmed
        return None

    @staticmethod
    def _location_variants(base: str, taxonomy: frozenset[str]) -> tuple[str, ...]:
        """Same-role slugs addressed to a US place.

        Every returned slug begins with ``base``, so this widens *where* the
        role is searched and never *which* role.
        """
        prefix = f"{base}-in-"
        return tuple(
            sorted(
                slug
                for slug in taxonomy
                if slug.startswith(prefix)
                and is_us_location_slug(slug[len(prefix) :])
            )
        )

    @classmethod
    def _match_slugs(
        cls, query: str, taxonomy: frozenset[str], *, max_slugs: int
    ) -> tuple[str, ...]:
        """The taxonomy pages to read for one role title.

        The base slug always comes first. Ordering is alphabetical and so
        deterministic: the same resume searched twice reads the same pages.
        """
        base = cls._match_slug(query, taxonomy)
        if base is None:
            return ()
        if max_slugs <= 1:
            return (base,)
        variants = cls._location_variants(base, taxonomy)
        return (base, *variants[: max_slugs - 1])

    # --- Parsing -----------------------------------------------------------

    def _extract_records(self, html: str) -> list[dict[str, Any]]:
        """Pull the job list out of the page's embedded data."""
        match = _NEXT_DATA_RE.search(html)
        if match is None:
            raise PortalParseError(
                "Jobright page did not contain the expected embedded data.",
                portal=self.slug,
            )
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise PortalParseError(
                f"Jobright embedded data was not valid JSON: {exc.msg}.",
                portal=self.slug,
            ) from exc

        job_list = (
            payload.get("props", {}).get("pageProps", {}).get("jobList")
        )
        if job_list is None:
            # A page with no matches is legitimate; a page with no jobList key
            # at all means the shape changed and should be surfaced.
            raise PortalParseError(
                "Jobright page data contained no jobList.", portal=self.slug
            )
        return [record for record in job_list if isinstance(record, dict)]

    def _to_raw_job(self, record: dict[str, Any]) -> RawJob | None:
        """Map one Jobright record onto the shared RawJob contract.

        Returns None when the record lacks the minimum needed to be usable —
        an id, a title and a URL. A listing the user cannot open is not a
        result.
        """
        result = record.get("jobResult") or {}
        company = record.get("companyResult") or {}

        external_id = result.get("jobId")
        title = (result.get("jobTitle") or "").strip()
        source_url = result.get("url") or result.get("applyLink")

        if not external_id or not title or not source_url:
            logger.debug(
                "jobright_record_skipped",
                portal=self.slug,
                has_id=bool(external_id),
                has_title=bool(title),
                has_url=bool(source_url),
            )
            return None

        return RawJob(
            external_id=str(external_id),
            source=self.slug,
            title=title,
            company=(company.get("companyName") or "").strip() or None,
            location=self._location(result),
            description=self._description(result),
            source_url=source_url,
            # Jobright's own job page: the usable link a user opens to apply.
            application_url=result.get("applyLink") or source_url,
            posted_at=self._posted_at(result.get("publishTime")),
            employment_type=(result.get("employmentType") or None),
            raw_payload=declare_application_type(
                dict(record), self._application_type(result)
            ),
        )

    @staticmethod
    def _application_type(result: dict[str, Any]) -> ApplicationType:
        """What the record says about how the user will apply (Phase 13).

        Jobright publishes ``jobtargetEasyapply`` on its job records - its own
        flag for a one-click submission rather than a hand-off to the
        employer's form. ``True`` is refused.

        ``False`` is reported as a long application because Jobright is
        asserting the opposite, not merely staying silent. A record without the
        field is left ``UNKNOWN`` rather than assumed either way.
        """
        flag = result.get("jobtargetEasyapply")
        if flag is True:
            return ApplicationType.QUICK_APPLY
        if flag is False:
            return ApplicationType.LONG_APPLICATION
        return ApplicationType.UNKNOWN

    @staticmethod
    def _location(result: dict[str, Any]) -> str | None:
        location = result.get("jobLocation")
        if isinstance(location, str) and location.strip():
            return location.strip()
        locations = result.get("jobLocations")
        if isinstance(locations, list) and locations:
            joined = ", ".join(str(item) for item in locations if item)
            return joined or None
        return None

    @staticmethod
    def _description(result: dict[str, Any]) -> str | None:
        """Compose the description Jobright publishes.

        Summary, responsibilities and requirements are separate fields; the
        matching layer wants one body of text, and dropping the requirements
        would discard exactly what the mandatory-skill checks need later.
        """
        sections: list[str] = []

        summary = (result.get("jobSummary") or "").strip()
        if summary:
            sections.append(summary)

        for label, key in (
            ("Responsibilities", "coreResponsibilities"),
            ("Requirements", "requirements"),
        ):
            items = result.get(key)
            if isinstance(items, list) and items:
                bullets = "\n".join(f"- {item}" for item in items if item)
                if bullets:
                    sections.append(f"{label}:\n{bullets}")

        return "\n\n".join(sections) or None

    @staticmethod
    def _posted_at(value: Any) -> datetime | None:
        """Parse Jobright's publish time, which carries no timezone marker."""
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.strptime(value.strip(), _PUBLISH_TIME_FORMAT)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC)


__all__ = [
    "BASE_URL",
    "MAX_SLUGS_PER_ROLE",
    "FreshnessWindow",
    "JobrightPortal",
    "slugify_role",
]
