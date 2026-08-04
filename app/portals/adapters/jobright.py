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
from app.core.logging import get_logger
from app.portals.base import (
    BasePortal,
    PortalCapabilities,
    PortalHealth,
    PortalSearchRequest,
    PortalSearchResult,
    RawJob,
)
from app.portals.exceptions import PortalParseError
from app.portals.http import HttpPolicy, PortalHttpClient

logger = get_logger(__name__)

BASE_URL = "https://jobright.ai"
TAXONOMY_SITEMAP = f"{BASE_URL}/sitemap-taxonomy.xml"

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
        taxonomy = await self._load_taxonomy()
        seen: set[str] = set()
        jobs: list[RawJob] = []

        for query in request.queries:
            if len(jobs) >= request.limit:
                break

            slug = self._match_slug(query, taxonomy)
            if slug is None:
                logger.info(
                    "jobright_no_taxonomy_match", portal=self.slug, query=query
                )
                continue

            html = await self._http.get_text(f"{BASE_URL}/jobs/{slug}")
            for record in self._extract_records(html):
                raw = self._to_raw_job(record)
                if raw is None or raw.external_id in seen:
                    continue
                seen.add(raw.external_id or raw.source_url)
                jobs.append(raw)
                if len(jobs) >= request.limit:
                    break

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            # The surface does not page, so there is never more to fetch.
            has_more=False,
            next_cursor=None,
            window_applied_by_portal=False,
        )

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

        # e.g. "senior-financial-analyst" -> "financial-analyst"
        parts = candidate.split("-")
        for start in range(len(parts)):
            trimmed = "-".join(parts[start:])
            if trimmed in taxonomy:
                return trimmed
        return None

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
            raw_payload=record,
        )

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


__all__ = ["BASE_URL", "FreshnessWindow", "JobrightPortal", "slugify_role"]
