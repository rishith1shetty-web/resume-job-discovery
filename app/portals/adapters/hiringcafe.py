"""Hiring Cafe adapter (https://hiringcafe.com/).

Access surface, and why this one
--------------------------------
Hiring Cafe's ``robots.txt`` allows ``/jobs`` and ``/jobs/`` and explicitly
disallows the site's own search surface::

    Disallow: /*?searchState=*
    Disallow: /*?page=*
    Disallow: /*&page=*
    Disallow: /viewjob/
    Disallow: /org/
    Disallow: /company/
    Disallow: /b/

So this adapter does **not** drive the site's search box, which is what
``?searchState=`` is. It reads only the server-rendered taxonomy pages at
``https://hiringcafe.com/jobs/<slug>``, which are listed in the site's own
``jobs-sitemap.xml`` and are explicitly allowed. Those pages embed their
listings in ``__NEXT_DATA__``.

Known limitations, stated up front
----------------------------------
* **No pagination.** ``?page=`` is disallowed, so one taxonomy page yields the
  20 listings the server rendered (``ssrPageSize``) however many exist -
  ``ssrTotalCount`` is frequently in the thousands. Breadth comes from querying
  several same-role taxonomy slugs, not from paging.
* **Partial role coverage.** The taxonomy has roughly 4,500 slugs, many of them
  location variants of a base role. A role with no matching slug returns
  nothing rather than a wrong match.
* **Descriptions are composed, not verbatim.** Hiring Cafe does not publish the
  employer's posting text on this surface. It publishes structured fields -
  a requirements summary, role activities, tools, degree and experience
  requirements - and this adapter assembles those into the description. It is
  Hiring Cafe's structured summary of the posting, not the employer's own
  wording.
* **No per-job page.** ``/job/<id>`` does not resolve and ``/viewjob/`` is
  disallowed, so the listing's ``source_url`` is the taxonomy page it was found
  on. The *application* URL is the employer's own ATS link, which is more
  direct than any board page would be.

Privacy
-------
``job_information`` carries ``viewedByUsers`` and ``appliedFromUsers``: arrays
of other people's account identifiers. Those are stripped before the payload is
stored. They are nobody's business here, and ``raw_payload`` is persisted.

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

BASE_URL = "https://hiringcafe.com"
TAXONOMY_SITEMAP = f"{BASE_URL}/jobs-sitemap.xml"

#: The page data is embedded in this script tag by the site's framework.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
_SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>")

#: Account identifiers of other users. Never stored.
_PRIVATE_FIELDS = ("viewedByUsers", "appliedFromUsers")

#: Composed descriptions shorter than this are not worth returning: the
#: matching layer would reject them as unscoreable anyway, and a stub would
#: look like a real posting.
_MIN_USEFUL_DESCRIPTION = 80


def slugify_role(role: str) -> str:
    """Turn a role title into Hiring Cafe's taxonomy slug form.

    ``"Financial Analyst"`` -> ``"financial-analyst"``.
    """
    lowered = role.strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-")


class HiringCafePortal(BasePortal):
    """Collects listings from Hiring Cafe's public taxonomy pages."""

    slug: ClassVar[str] = "hiringcafe"
    display_name: ClassVar[str] = "Hiring Cafe"

    #: Filled from the sitemap on first use so role -> slug matching is driven
    #: by Hiring Cafe's own taxonomy rather than a list hardcoded here.
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
            # Everything available arrives with the listing; there is no
            # allowed per-job page to fetch.
            supports_description_fetch=False,
            # `?page=` is disallowed by robots.txt.
            supports_pagination=False,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings for the request's same-role title variations.

        Each query is matched to a taxonomy slug and fetched independently, so
        one unmatched or broken slug cannot lose the whole pass.
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
                    "hiringcafe_no_taxonomy_match", portal=self.slug, query=query
                )
                continue

            html = await self._http.get_text(f"{BASE_URL}/jobs/{slug}")
            for record in self._extract_records(html):
                raw = self._to_raw_job(record, slug=slug)
                if raw is None:
                    continue
                identity = raw.external_id or raw.source_url
                if identity in seen:
                    continue
                seen.add(identity)
                jobs.append(raw)
                if len(jobs) >= request.limit:
                    break

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            # The allowed surface does not page, so there is never more to
            # fetch for a given slug.
            has_more=False,
            next_cursor=None,
            window_applied_by_portal=False,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        """Descriptions already arrive with the listing.

        There is no allowed per-job page: ``/job/<id>`` does not resolve and
        ``/viewjob/`` is disallowed, so there is nothing further to fetch
        without crossing that line.
        """
        return job.description

    async def validate_url(self, url: str) -> bool:
        return await self._http.head_ok(url)

    async def health_check(self) -> PortalHealth:
        reachable = await self._http.head_ok(f"{BASE_URL}/jobs/software-engineer")
        return PortalHealth(
            is_available=reachable,
            checked_at=datetime.now(UTC),
            detail=None if reachable else "Taxonomy page did not respond.",
        )

    # --- Taxonomy ----------------------------------------------------------

    async def _load_taxonomy(self) -> frozenset[str]:
        """Fetch and cache Hiring Cafe's taxonomy slugs from its sitemap."""
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
                "Hiring Cafe taxonomy sitemap contained no job URLs.",
                portal=self.slug,
            )

        type(self)._taxonomy_cache = frozenset(slugs)
        logger.info("hiringcafe_taxonomy_loaded", portal=self.slug, count=len(slugs))
        return type(self)._taxonomy_cache

    @staticmethod
    def _match_slug(query: str, taxonomy: frozenset[str]) -> str | None:
        """Best taxonomy slug for a role title, or None.

        Exact match first, then the longest suffix that exists - so "Senior
        Financial Analyst" falls back to "financial-analyst". Returning None is
        correct when nothing fits: a wrong slug would mean searching a
        different role, which the product forbids.
        """
        candidate = slugify_role(query)
        if not candidate:
            return None
        if candidate in taxonomy:
            return candidate

        parts = candidate.split("-")
        for start in range(len(parts)):
            trimmed = "-".join(parts[start:])
            if trimmed in taxonomy:
                return trimmed
        return None

    # --- Parsing -----------------------------------------------------------

    def _extract_records(self, html: str) -> list[dict[str, Any]]:
        """Pull the listing array out of the page's embedded data."""
        match = _NEXT_DATA_RE.search(html)
        if match is None:
            raise PortalParseError(
                "Hiring Cafe page did not contain the expected embedded data.",
                portal=self.slug,
            )
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise PortalParseError(
                f"Hiring Cafe embedded data was not valid JSON: {exc.msg}.",
                portal=self.slug,
            ) from exc

        hits = payload.get("props", {}).get("pageProps", {}).get("ssrHits")
        if hits is None:
            # A page with no matches is legitimate; a page with no ssrHits key
            # at all means the shape changed and should be surfaced.
            raise PortalParseError(
                "Hiring Cafe page data contained no ssrHits.", portal=self.slug
            )
        return [record for record in hits if isinstance(record, dict)]

    def _to_raw_job(self, record: dict[str, Any], *, slug: str) -> RawJob | None:
        """Map one Hiring Cafe record onto the shared RawJob contract.

        Returns None when the record lacks the minimum needed to be usable - an
        id, a title and somewhere to apply - or when it is already expired. A
        listing the user cannot open is not a result.
        """
        if record.get("is_expired") is True:
            return None

        info = record.get("job_information") or {}
        processed = record.get("v5_processed_job_data") or {}
        company_data = record.get("enriched_company_data") or {}

        external_id = record.get("id") or record.get("objectID")
        title = (info.get("title") or info.get("job_title_raw") or "").strip()
        apply_url = (record.get("apply_url") or "").strip()

        if not external_id or not title or not apply_url:
            logger.debug(
                "hiringcafe_record_skipped",
                portal=self.slug,
                has_id=bool(external_id),
                has_title=bool(title),
                has_apply_url=bool(apply_url),
            )
            return None

        return RawJob(
            external_id=str(external_id),
            source=self.slug,
            title=title,
            company=(company_data.get("name") or "").strip() or None,
            location=self._location(processed),
            description=self._description(processed),
            # Where the listing was found: the robots-allowed taxonomy page.
            source_url=f"{BASE_URL}/jobs/{slug}",
            # Where the user actually applies - the employer's own ATS.
            application_url=apply_url,
            posted_at=self._posted_at(processed.get("estimated_publish_date")),
            employment_type=self._employment_type(processed),
            raw_payload=self._safe_payload(record),
        )

    @staticmethod
    def _safe_payload(record: dict[str, Any]) -> dict[str, Any]:
        """The record with other users' identifiers removed.

        ``raw_payload`` is persisted, and ``viewedByUsers`` /
        ``appliedFromUsers`` are arrays of other people's account ids. They are
        of no use here and are not ours to store.
        """
        payload = dict(record)
        info = payload.get("job_information")
        if isinstance(info, dict):
            payload["job_information"] = {
                key: value
                for key, value in info.items()
                if key not in _PRIVATE_FIELDS
            }
        return payload

    @staticmethod
    def _location(processed: dict[str, Any]) -> str | None:
        """Human-readable workplace location.

        Prefers the formatted string Hiring Cafe publishes; falls back to the
        city list, then the state list, so a remote posting with no city still
        says something true.
        """
        formatted = processed.get("formatted_workplace_location")
        if isinstance(formatted, str) and formatted.strip():
            return formatted.strip()

        for key in ("workplace_cities", "workplace_states", "workplace_countries"):
            values = processed.get(key)
            if isinstance(values, list) and values:
                joined = ", ".join(str(item) for item in values if item)
                if joined:
                    return joined

        return None

    @classmethod
    def _description(cls, processed: dict[str, Any]) -> str | None:
        """Compose a description from the structured fields.

        Hiring Cafe does not publish the employer's posting text on this
        surface, so this assembles what it does publish. Sections are labelled
        so the result reads as the summary it is rather than as the employer's
        own words, and so the matching layer's requirement extraction has the
        headings it looks for.
        """
        sections: list[str] = []

        summary = (processed.get("requirements_summary") or "").strip()
        if summary:
            sections.append(f"Requirements:\n{summary}")

        activities = processed.get("role_activities")
        if isinstance(activities, list) and activities:
            bullets = "\n".join(f"- {item}" for item in activities if item)
            if bullets:
                sections.append(f"Responsibilities:\n{bullets}")

        tools = processed.get("technical_tools")
        if isinstance(tools, list) and tools:
            joined = ", ".join(str(item) for item in tools if item)
            if joined:
                sections.append(f"Tools and technologies: {joined}")

        certifications = processed.get("licenses_or_certifications")
        if isinstance(certifications, list) and certifications:
            joined = ", ".join(str(item) for item in certifications if item)
            if joined:
                sections.append(f"Licenses or certifications: {joined}")

        education = cls._education_line(processed)
        if education:
            sections.append(education)

        experience = cls._experience_line(processed)
        if experience:
            sections.append(experience)

        for label, key in (
            ("Job category", "job_category"),
            ("Seniority level", "seniority_level"),
            ("Workplace type", "workplace_type"),
        ):
            value = processed.get(key)
            if isinstance(value, str) and value.strip():
                sections.append(f"{label}: {value.strip()}")

        composed = "\n\n".join(sections)
        return composed if len(composed) >= _MIN_USEFUL_DESCRIPTION else None

    @staticmethod
    def _education_line(processed: dict[str, Any]) -> str | None:
        """State a degree requirement only when one is actually required.

        Hiring Cafe uses "Not Mentioned" and "Not Required" as distinct values;
        neither is a requirement, and claiming one would invent a hard reject
        the posting never made.
        """
        levels = (
            ("Doctorate", "doctorate_degree_requirement"),
            ("Master's", "masters_degree_requirement"),
            ("Bachelor's", "bachelors_degree_requirement"),
            ("Associate's", "associates_degree_requirement"),
        )
        for label, key in levels:
            value = processed.get(key)
            if isinstance(value, str) and "required" in value.lower():
                return f"Education: {label} degree {value.strip().lower()}."
        return None

    @staticmethod
    def _experience_line(processed: dict[str, Any]) -> str | None:
        """Years of experience, when the posting stated a number."""
        if processed.get("is_min_industry_and_role_yoe_not_mentioned") is True:
            return None

        years = processed.get("min_industry_and_role_yoe")
        if not isinstance(years, int | float) or years <= 0:
            return None

        return f"Experience: {years:g}+ years of relevant experience required."

    @staticmethod
    def _employment_type(processed: dict[str, Any]) -> str | None:
        """First stated commitment, e.g. "Full Time"."""
        commitment = processed.get("commitment")
        if isinstance(commitment, list) and commitment:
            first = str(commitment[0]).strip()
            return first or None
        if isinstance(commitment, str) and commitment.strip():
            return commitment.strip()
        return None

    @staticmethod
    def _posted_at(value: Any) -> datetime | None:
        """Parse Hiring Cafe's ISO-8601 publish time.

        The field is named ``estimated_publish_date`` and is exactly that - an
        estimate. It is passed through unchanged rather than adjusted, and a
        listing without one keeps ``posted_at`` empty rather than being given a
        made-up date.
        """
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = ["BASE_URL", "FreshnessWindow", "HiringCafePortal", "slugify_role"]
