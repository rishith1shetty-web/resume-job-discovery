"""Scale.jobs adapter (https://scale.jobs/).

Access surface, and why this one
--------------------------------
Scale.jobs is a job-application service whose public site also publishes a job
search. The search surface and the job data live on **two different hosts**,
and they carry two different policies, so both were checked.

``scale.jobs`` (the site) says::

    User-agent: *
    Allow: /
    Disallow: /sign-in    Disallow: /sign-up
    Disallow: /dashboard/ Disallow: /api/

So the site's own ``/api/`` is closed, and its ``llms.txt`` files
``https://scale.jobs/api`` under "Authentication & Private Pages (#ignore)".
Nothing on that host is called for job data. It could not be used anyway: the
``/jobs`` and ``/jobs/<role>`` pages are client-rendered shells that ship the
literal text "Loading jobs..." and carry no listing in their HTML at all.

The job data comes from a **separate host**, ``jobhose-prod.scale.jobs``, which
is what the site's own client calls. robots.txt is per host, and that host
serves no robots.txt (404), so no rule there restricts this client. Its
``GET /api/search-jobs`` endpoint is self-documented at ``/api/docs``, needs no
authentication, no key and no session, and the site's client calls it with the
literal parameter ``userId=public-search`` under a React Query key named
``public-jobs``. It answers this adapter's honest ``JobFinderBot`` user agent
with a 200.

Two constraints this adapter enforces at the request itself
-----------------------------------------------------------
The documented endpoint is a superset of what this project may collect, so the
limits are applied as query parameters rather than by filtering afterwards:

* **No LinkedIn.** The publisher's own docs say the inventory carries
  LinkedIn-sourced jobs and offers ``excludeLinkedIn=true`` to omit them. This
  project does not collect LinkedIn (its robots.txt refuses this client), so
  that flag is sent on every request, always, and is not configurable.
* **No Easy Apply.** The docs say LinkedIn Easy Apply listings
  (``directapply=true``) are excluded by default and that
  ``excludeEasyApply=false`` would include them. That parameter is never sent,
  so the default stands. This project has no apply path of any kind.

The same host also exposes administrative endpoints — ``/api/sync/*``,
``/api/cron/start``, ``/api/cron/stop``, ``/api/expired/mark``,
``/api/embeddings/backfill``. Every one of them mutates the publisher's data.
This adapter issues **read-only GETs to /api/search-jobs and nothing else**,
and a test asserts exactly that.

What this portal gives
----------------------
* **Employer-direct application links.** Every observed listing carries
  ``sourceType: "ats"`` and a ``url`` pointing at the employer's own applicant
  tracking system - Workable, Workday, SmartRecruiters, iCIMS, Greenhouse,
  UKG and others. Those rank above an aggregator listing in the preferred-URL
  order, so the user lands where the application is actually submitted.
* **A real date filter.** ``dateFrom`` genuinely restricts results, so this is
  one of the few portals here that can be asked for a freshness window instead
  of being filtered locally. The inventory is deep enough that the narrowest
  window still fills a quota, so the ladder normally stops at the first rung.
* **Real pagination.** ``skip``/``take`` returns non-overlapping pages.

Descriptions, stated honestly
-----------------------------
``descriptionText`` is **not the employer's verbatim posting**. It is a
model-written summary produced by the publisher, and it is consistently short -
roughly 270 to 470 characters, against a 200 character floor for a job to be
scoreable at all. It already reads as the requirements summary, the tool list
and the responsibilities run together, because those are the same three fields
the publisher composed it from; re-appending them would pad the text without
adding a word of information.

So this adapter appends only facts the summary does **not** already contain -
compensation, arrangement, employment type, experience, sponsorship, location -
and gates on the length of the *publisher's own text*, never on the composed
total. A listing that arrives without a real summary is returned without a
description and is rejected downstream as unscoreable, which is the correct
outcome: padding it to clear the floor would defeat the floor.

There is no per-job endpoint to fetch a fuller description from, and no per-job
permalink on scale.jobs to link to, so ``supports_description_fetch`` is false
and the listing links straight to the employer's ATS.

This adapter collects and normalises. It does not score, deduplicate, rank or
apply to anything.
"""

from __future__ import annotations

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
)
from app.portals.exceptions import PortalParseError
from app.portals.http import HttpPolicy, PortalHttpClient
from app.processing.us_locations import COUNTRY_NAMES_BY_CODE
from app.search.freshness import cutoff_for

logger = get_logger(__name__)


def _positive_int(value: Any, default: int) -> int:
    """A configured positive integer, or the default.

    A missing, non-numeric or non-positive value falls back rather than
    disabling the bound: a misconfigured row must not create an endless search.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if value > 0 else default

#: The publisher's own search host. Not scale.jobs itself, whose /api/ is
#: disallowed and whose job pages carry no data.
API_BASE = "https://jobhose-prod.scale.jobs"
SEARCH_URL = f"{API_BASE}/api/search-jobs"

#: The identity the site's own client sends for an unauthenticated search.
PUBLIC_USER_ID = "public-search"

#: Listings per request. The endpoint's own client uses 20.
PAGE_SIZE = 20

#: Default pages per query per window. Phase 12 searches deeply rather than to
#: a quota, so this is the *default* rather than a ceiling: raise the portal's
#: ``max_pages`` option to collect further without a deployment. The collector's
#: own request, runtime and job limits still bound the leg.
MAX_PAGES = 2

#: Floor applied to the publisher's summary alone. Below this there is no real
#: posting text, and the structured extras must not be allowed to make up the
#: difference - see the module docstring.
_MIN_USEFUL_DESCRIPTION = 80


class ScaleJobsPortal(BasePortal):
    """Collects listings from Scale.jobs' public job search endpoint."""

    slug: ClassVar[str] = "scalejobs"
    display_name: ClassVar[str] = "Scale.jobs"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug, policy=HttpPolicy.from_options(self.options)
        )
        self._max_pages = _positive_int(self.options.get("max_pages"), MAX_PAGES)

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # `dateFrom` genuinely restricts results, verified against the live
            # endpoint, so windows are asked for rather than filtered locally.
            supports_date_filter=True,
            # No per-job endpoint exists; the summary collected during search is
            # all the text this portal publishes.
            supports_description_fetch=False,
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
        now = datetime.now(UTC)
        seen: set[str] = set()
        jobs: list[RawJob] = []

        for query in request.queries:
            if len(jobs) >= request.limit:
                break

            for page in range(self._max_pages):
                if len(jobs) >= request.limit:
                    break

                payload = await self._search_once(
                    query,
                    window=request.window,
                    skip=page * PAGE_SIZE,
                    now=now,
                )
                records = payload["jobs"]
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

                if not payload["has_more"]:
                    break

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            has_more=False,
            next_cursor=None,
            # The endpoint applied the window itself via `dateFrom`.
            window_applied_by_portal=True,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        """No fuller text exists to fetch.

        The publisher exposes no per-job endpoint, so the summary composed
        during ``search`` is what there is. Returning it unchanged is honest;
        inventing a second request that cannot succeed is not.
        """
        return job.description

    async def validate_url(self, url: str) -> bool:
        return await self._http.head_ok(url)

    async def health_check(self) -> PortalHealth:
        """Probe the surface actually used.

        The host's ``/health`` route is not used: it reports on the publisher's
        own database and currently answers 500 after a 30 second wait, which
        says nothing about whether search works for us.
        """
        reachable = await self._http.head_ok(SEARCH_URL)
        return PortalHealth(
            is_available=reachable,
            checked_at=datetime.now(UTC),
            detail=None if reachable else "Search endpoint did not respond.",
        )

    # --- Fetching ----------------------------------------------------------

    def _params(
        self, query: str, *, window: FreshnessWindow, skip: int, now: datetime
    ) -> dict[str, Any]:
        """Query parameters for one page of one search.

        ``excludeLinkedIn`` is unconditional: see the module docstring. No
        parameter here re-enables Easy Apply listings, which the endpoint
        excludes by default.
        """
        params: dict[str, Any] = {
            "userId": PUBLIC_USER_ID,
            "skip": skip,
            "take": PAGE_SIZE,
            "jobTitles": query,
            "source": "live",
            "excludeLinkedIn": "true",
        }

        cutoff = cutoff_for(window, now)
        if cutoff is not None:
            # OLDER_ACTIVE has no cutoff and so sends no date filter at all.
            params["dateFrom"] = cutoff.date().isoformat()

        return params

    async def _search_once(
        self, query: str, *, window: FreshnessWindow, skip: int, now: datetime
    ) -> dict[str, Any]:
        """One page of results, plus whether the endpoint has more."""
        # A comma separates titles in this parameter, so a title containing one
        # would silently become two searches. Collapse it, and the whitespace
        # it leaves behind, into a single query.
        cleaned = " ".join(query.replace(",", " ").split())
        if not cleaned:
            return {"jobs": [], "has_more": False}

        payload = await self._http.get_json(
            SEARCH_URL,
            params=self._params(cleaned, window=window, skip=skip, now=now),
        )

        records = payload.get("jobs")
        if records is None:
            # A search with no matches returns an empty list, not a missing
            # key. A missing key means the shape changed and must be surfaced.
            raise PortalParseError(
                "Scale.jobs response contained no jobs key.", portal=self.slug
            )
        if not isinstance(records, list):
            raise PortalParseError(
                "Scale.jobs search results were not a list.", portal=self.slug
            )

        return {
            "jobs": [record for record in records if isinstance(record, dict)],
            "has_more": bool(payload.get("hasMore")),
        }

    # --- Parsing -----------------------------------------------------------

    def _to_raw_job(self, record: dict[str, Any]) -> RawJob | None:
        """Map one Scale.jobs record onto the shared RawJob contract.

        Returns None when the record lacks the minimum needed to be usable - an
        identity, a title and a link. A listing the user cannot open is not a
        result.
        """
        external_id = str(record.get("id") or record.get("externalId") or "").strip()
        title = str(record.get("title") or "").strip()
        url = str(record.get("url") or "").strip()

        if not external_id or not title or not url:
            logger.debug(
                "scalejobs_record_skipped",
                portal=self.slug,
                has_id=bool(external_id),
                has_title=bool(title),
                has_url=bool(url),
            )
            return None

        return RawJob(
            external_id=external_id,
            source=self.slug,
            title=title,
            company=str(record.get("organization") or "").strip() or None,
            location=self._location(record),
            description=self._description(record),
            # Scale.jobs publishes no per-job permalink of its own, so both
            # point at the employer's ATS - which is where the user applies and
            # ranks above an aggregator link anyway.
            source_url=url,
            application_url=url,
            posted_at=self._posted_at(record.get("datePosted")),
            employment_type=self._employment_type(record),
            raw_payload=declare_application_type(
                dict(record), self._application_type(record)
            ),
        )

    @staticmethod
    def _application_type(record: dict[str, Any]) -> ApplicationType:
        """What the record says about where the user will apply (Phase 13).

        Scale.jobs publishes ``sourceType`` per listing, and every one of the
        80 records in the captured fixture is ``ats`` - the destination is an
        employer applicant tracking system (Workday, iCIMS, UltiPro,
        SuccessFactors and so on), which is a full application by definition.

        This reads that field rather than declaring the whole portal a long
        application. The two are indistinguishable today, but they stop being
        so the moment Scale.jobs indexes a source that is not an ATS, and at
        that point this returns ``UNKNOWN`` and the shared classifier looks at
        the destination itself instead of waving the listing through.
        """
        source_type = str(record.get("sourceType") or "").strip().casefold()
        if source_type == "ats":
            return ApplicationType.LONG_APPLICATION
        return ApplicationType.UNKNOWN

    @classmethod
    def _description(cls, record: dict[str, Any]) -> str | None:
        """The publisher's summary, plus facts it does not already state.

        The gate is on the summary alone. ``descriptionText`` already contains
        the requirements, tool list and responsibilities the publisher composed
        it from, so those are not repeated here; what is appended is the
        structured detail that appears nowhere in the prose.
        """
        summary = cls._clean(record.get("descriptionText"))
        if not summary or len(summary) < _MIN_USEFUL_DESCRIPTION:
            # No real posting text. Downstream will reject this as unscoreable,
            # which is correct - padding it to clear the floor would defeat it.
            return None

        sections: list[str] = [summary]

        for label, value in (
            ("Employment type", cls._clean(record.get("aiEmploymentType"))),
            ("Work arrangement", cls._clean(record.get("aiWorkArrangement"))),
            ("Experience level", cls._clean(record.get("aiExperienceLevel"))),
        ):
            if value:
                sections.append(f"{label}: {value}")

        minimum = record.get("aiMinExperience")
        if isinstance(minimum, int | float) and not isinstance(minimum, bool):
            sections.append(f"Minimum experience: {minimum:g} years")

        salary = cls._salary(record)
        if salary:
            sections.append(f"Compensation: {salary}")

        if record.get("aiVisaSponsorship") is True:
            sections.append("Visa sponsorship: stated as available")

        location = cls._location(record)
        if location:
            sections.append(f"Location: {location}")

        return "\n\n".join(sections)

    @staticmethod
    def _salary(record: dict[str, Any]) -> str | None:
        """Annual range as published, when published."""

        def number(key: str) -> float | None:
            value = record.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float):
                return None
            return float(value) if value > 0 else None

        low = number("aiSalaryAnnualMin")
        high = number("aiSalaryAnnualMax")
        if low is None and high is None:
            return None

        currency = str(record.get("aiSalaryCurrency") or "").strip()
        if low is not None and high is not None:
            amount = f"{low:,.0f} - {high:,.0f}"
        else:
            amount = f"{(low if low is not None else high):,.0f}"

        return f"{currency} {amount}".strip() + " per year"

    @classmethod
    def _location(cls, record: dict[str, Any]) -> str | None:
        """A readable place, from whichever field carries one.

        ``jobLocations`` is structured and preferred; the raw alternatives are
        used only when it is absent.

        The country arrives as an ISO alpha-2 code, and the city and state
        strings carry it as a suffix ("Cascais, Lisbon, PT"). Those codes are
        resolved to names here, at the one place that knows the field is a
        country: half of them are also US state abbreviations - this endpoint
        really does publish ``{"country": "MT"}`` for a job in Malta - and a
        bare code reaching the US filter would read as Montana.
        """
        entries = record.get("jobLocations")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue

                code = (cls._clean(entry.get("country")) or "").upper()
                country = COUNTRY_NAMES_BY_CODE.get(code)
                suffix = f", {code}"

                parts: list[str] = []
                for key in ("city", "state"):
                    cleaned = cls._clean(entry.get(key))
                    if not cleaned:
                        continue
                    if country and cleaned.upper().endswith(suffix.upper()):
                        # Drop the code; the resolved name is appended below.
                        cleaned = cleaned[: -len(suffix)].strip().rstrip(",")
                    if cleaned:
                        parts.append(cleaned)

                parts.append(country or cls._clean(entry.get("country")) or "")
                parts = [part for part in parts if part]
                if not parts:
                    continue

                # "Evansville, Indiana" already contains its state, so drop any
                # part another part already spells out.
                kept: list[str] = []
                for part in parts:
                    if not any(part in existing for existing in kept):
                        kept.append(part)
                return ", ".join(kept)

        for key in ("locationsRaw", "locationsAltRaw", "locationRequirementsRaw"):
            value = record.get(key)
            if isinstance(value, str) and (cleaned := cls._clean(value)):
                return cleaned
            if isinstance(value, list):
                joined = ", ".join(
                    cleaned for item in value if (cleaned := cls._clean(item))
                )
                if joined:
                    return joined

        if record.get("isRemote") is True:
            return "Remote"

        return None

    @staticmethod
    def _employment_type(record: dict[str, Any]) -> str | None:
        for key in ("employmentType", "aiEmploymentType"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and value:
                first = str(value[0]).strip()
                if first:
                    return first
        return None

    @staticmethod
    def _clean(value: Any) -> str | None:
        """Tidy one of the endpoint's text fields."""
        if not isinstance(value, str):
            return None
        return " ".join(value.split()).strip() or None

    @staticmethod
    def _posted_at(value: Any) -> datetime | None:
        """Convert the endpoint's ISO-8601 timestamp.

        A listing without a usable one keeps ``posted_at`` empty rather than
        being given a made-up date; the freshness ladder then admits it only at
        OLDER_ACTIVE.
        """
        if not isinstance(value, str) or not value.strip():
            return None

        text = value.strip()
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"

        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None

        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "API_BASE",
    "MAX_PAGES",
    "PAGE_SIZE",
    "PUBLIC_USER_ID",
    "SEARCH_URL",
    "ScaleJobsPortal",
]
