"""LinkedIn adapter.

Access status — read this before enabling the portal
----------------------------------------------------
As of the last check, ``https://www.linkedin.com/robots.txt`` says, for the
generic user agent:

    User-agent: *
    Disallow: /

That is a blanket refusal covering every path, including the guest job-search
endpoints. LinkedIn's own notice in that file directs anyone wanting to crawl
to apply for whitelisting at ``whitelist-crawl@linkedin.com``.

So this adapter **does not collect from LinkedIn by default**. Its parsing,
mapping and filtering are fully implemented and tested against fixtures, so the
moment access is authorised it works — but the network path is closed behind an
explicit operator assertion:

    portal_configs.options = {"access_authorized": true}

Setting that flag is a statement by the operator that they hold LinkedIn's
permission (whitelisting, a partner agreement, or an equivalent licence). It is
not a bypass: nothing here defeats bot protection, spoofs fingerprints, solves
CAPTCHAs or disguises the client. If LinkedIn refuses the request, the adapter
reports the portal as blocked and stops.

Application rules enforced here
-------------------------------
Only listings that expose a **normal external apply link** are returned.
Anything that would have to be completed inside LinkedIn — its Easy Apply flow
— is dropped at the filter below. That detection exists for one purpose:
excluding those jobs from results. There is no code here that opens, fills,
submits or otherwise progresses an application, and there never will be
(docs/DECISIONS.md D-002).
"""

from __future__ import annotations

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
from app.portals.exceptions import PortalBlockedError, PortalParseError
from app.portals.http import HttpPolicy, PortalHttpClient

logger = get_logger(__name__)

BASE_URL = "https://www.linkedin.com"

#: Operators assert authorisation with this option. Absent or false means the
#: adapter will not make a request.
AUTHORIZATION_OPTION = "access_authorized"

ACCESS_NOTICE = (
    "LinkedIn's robots.txt disallows automated access for this client "
    "(User-agent: * / Disallow: /). Collection is therefore disabled. If you "
    "hold LinkedIn's written permission - whitelisting via "
    "whitelist-crawl@linkedin.com, a partner agreement or an equivalent "
    f"licence - set the portal option {AUTHORIZATION_OPTION!r} to true to "
    "enable it. No restriction is bypassed either way."
)

#: How LinkedIn labels an application that completes inside its own product.
#: Matched case-insensitively against the listing's apply method or label.
_IN_PLATFORM_APPLY_MARKERS = (
    "easyapply",
    "easy apply",
    "simpleonsiteapply",
    "complexonsiteapply",
    "onsiteapply",
)

_JOB_ID_RE = re.compile(r"(?:currentJobId=|/jobs/view/(?:[^/]*-)?)(\d{6,})")


def _collapse(value: str) -> str:
    """Lowercase and drop separators, so spellings compare equal."""
    return re.sub(r"[\s_\-]+", "", value).lower()


def is_in_platform_application(record: dict[str, Any]) -> bool:
    """True when applying would happen inside LinkedIn rather than externally.

    Used **only** to exclude such listings. The product never completes an
    application, so a job that cannot be applied to by opening an external link
    is not a usable result.
    """
    for key in ("applyMethod", "applyType", "applyLabel"):
        value = record.get(key)
        if isinstance(value, str):
            # Collapse separators on both sides so "Easy Apply", "easy_apply"
            # and "SimpleOnsiteApply" all compare equal to their marker.
            collapsed = _collapse(value)
            if any(
                _collapse(marker) in collapsed
                for marker in _IN_PLATFORM_APPLY_MARKERS
            ):
                return True

    # Explicit boolean, whatever LinkedIn happens to call it in a given shape.
    for key in ("easyApply", "isEasyApply"):
        if record.get(key) is True:
            return True

    return False


def external_apply_url(record: dict[str, Any]) -> str | None:
    """The employer-side apply URL, if this listing exposes one.

    A redirect onward to Workday, Greenhouse, Lever or a company careers page
    is expected and fine — the user opens it and applies themselves.
    """
    for key in ("companyApplyUrl", "applyUrl", "externalApplyUrl", "applyLink"):
        value = record.get(key)
        if isinstance(value, str) and value.strip().startswith("http"):
            return value.strip()
    return None


class LinkedInPortal(BasePortal):
    """LinkedIn listings, restricted to externally-applicable jobs."""

    slug: ClassVar[str] = "linkedin"
    display_name: ClassVar[str] = "LinkedIn"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug, policy=HttpPolicy.from_options(self.options)
        )

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            supports_date_filter=True,
            supports_description_fetch=True,
            supports_pagination=True,
            # True in the sense that matters operationally: this portal cannot
            # be collected from without prior authorisation.
            requires_auth=True,
            provides_application_url=True,
        )

    @property
    def is_access_authorized(self) -> bool:
        return self.options.get(AUTHORIZATION_OPTION) is True

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings, or refuse and say why.

        Raising rather than returning an empty result is deliberate: the run
        records a real reason against this portal and carries on with the
        others, instead of quietly reporting that LinkedIn had no jobs.
        """
        self._require_authorization()

        jobs: list[RawJob] = []
        excluded = 0

        for query in request.queries:
            if len(jobs) >= request.limit:
                break

            html = await self._http.get_text(
                f"{BASE_URL}/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params=self._search_params(query, request),
            )
            records, skipped = self.parse_search_results(html)
            excluded += skipped

            for record in records:
                raw = self._to_raw_job(record)
                if raw is None:
                    continue
                jobs.append(raw)
                if len(jobs) >= request.limit:
                    break

        if excluded:
            logger.info(
                "linkedin_in_platform_listings_excluded",
                portal=self.slug,
                excluded=excluded,
            )

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            has_more=False,
            next_cursor=None,
            window_applied_by_portal=True,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        self._require_authorization()
        return job.description

    async def validate_url(self, url: str) -> bool:
        if not self.is_access_authorized:
            # Do not probe a portal that has not authorised this client.
            return False
        return await self._http.head_ok(url)

    async def health_check(self) -> PortalHealth:
        """Report the access position rather than pinging a portal that said no."""
        if not self.is_access_authorized:
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=ACCESS_NOTICE,
            )
        reachable = await self._http.head_ok(f"{BASE_URL}/jobs")
        return PortalHealth(
            is_available=reachable,
            checked_at=datetime.now(UTC),
            detail=None if reachable else "LinkedIn did not respond.",
        )

    def _require_authorization(self) -> None:
        if not self.is_access_authorized:
            raise PortalBlockedError(ACCESS_NOTICE, portal=self.slug)

    def _search_params(
        self, query: str, request: PortalSearchRequest
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"keywords": query, "start": 0}
        if request.location:
            params["location"] = request.location
        seconds = _WINDOW_SECONDS.get(request.window)
        if seconds:
            params["f_TPR"] = f"r{seconds}"
        if request.remote_only:
            params["f_WT"] = 2
        return params

    # --- Parsing -----------------------------------------------------------

    def parse_search_results(
        self, html: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Parse job cards, dropping anything that applies inside LinkedIn.

        Returns ``(records, excluded_count)``. Pure and side-effect free, so it
        is tested directly against saved fixtures without any network access.
        """
        if not isinstance(html, str):
            raise PortalParseError(
                "LinkedIn response was not text.", portal=self.slug
            )

        records: list[dict[str, Any]] = []
        excluded = 0

        for card in _split_cards(html):
            record = _parse_card(card)
            if record is None:
                continue
            if is_in_platform_application(record):
                excluded += 1
                continue
            # Belt and braces: without an external link there is nothing the
            # user can open, so the listing is unusable regardless of label.
            if not external_apply_url(record):
                excluded += 1
                continue
            records.append(record)

        return records, excluded

    def _to_raw_job(self, record: dict[str, Any]) -> RawJob | None:
        title = (record.get("title") or "").strip()
        source_url = record.get("jobUrl")
        if not title or not source_url:
            return None

        return RawJob(
            external_id=record.get("jobId"),
            source=self.slug,
            title=title,
            company=(record.get("company") or "").strip() or None,
            location=(record.get("location") or "").strip() or None,
            description=record.get("description"),
            source_url=source_url,
            application_url=external_apply_url(record),
            posted_at=_parse_posted_at(record.get("postedAt")),
            employment_type=record.get("employmentType"),
            raw_payload=record,
        )


#: Freshness windows expressed as LinkedIn's `f_TPR` seconds parameter.
#: OLDER_ACTIVE is absent on purpose: it means "do not constrain by date".
_WINDOW_SECONDS: dict[FreshnessWindow, int] = {
    FreshnessWindow.LAST_24H: 86400,
    FreshnessWindow.LAST_3D: 259200,
    FreshnessWindow.LAST_7D: 604800,
    FreshnessWindow.LAST_14D: 1209600,
    FreshnessWindow.LAST_30D: 2592000,
}


def _split_cards(html: str) -> list[str]:
    """Split the guest search response into individual job cards."""
    chunks = re.split(r'<li\b', html)
    return [chunk for chunk in chunks[1:] if "base-card" in chunk or "job-search-card" in chunk]


def _parse_card(card: str) -> dict[str, Any] | None:
    """Extract one job card into a plain dict."""
    url_match = re.search(r'href="(https://[^"?]*?/jobs/view/[^"?]+)', card)
    if url_match is None:
        return None
    job_url = url_match.group(1)

    title = _text(re.search(r'class="[^"]*base-search-card__title[^"]*"[^>]*>(.*?)<', card, re.S))
    company = _text(re.search(r'class="[^"]*base-search-card__subtitle[^"]*"[^>]*>\s*(?:<a[^>]*>)?(.*?)<', card, re.S))
    location = _text(re.search(r'class="[^"]*job-search-card__location[^"]*"[^>]*>(.*?)<', card, re.S))
    posted = re.search(r'datetime="([^"]+)"', card)
    apply_label = _text(re.search(r'class="[^"]*job-search-card__easy-apply-label[^"]*"[^>]*>(.*?)<', card, re.S))
    external = re.search(r'data-external-apply-url="([^"]+)"', card)
    job_id = _JOB_ID_RE.search(job_url)

    record: dict[str, Any] = {
        "jobId": job_id.group(1) if job_id else None,
        "title": title,
        "company": company,
        "location": location,
        "jobUrl": job_url,
        "postedAt": posted.group(1) if posted else None,
    }
    if apply_label:
        record["applyLabel"] = apply_label
    if external:
        record["companyApplyUrl"] = external.group(1)
    return record


def _text(match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip() or None


def _parse_posted_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip()[: len(fmt) + 2], fmt).replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
    return None


__all__ = [
    "ACCESS_NOTICE",
    "AUTHORIZATION_OPTION",
    "BASE_URL",
    "LinkedInPortal",
    "external_apply_url",
    "is_in_platform_application",
]
