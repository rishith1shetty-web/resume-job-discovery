"""ZipRecruiter adapter (https://www.ziprecruiter.com/).

Access surface, and why this one
--------------------------------
ZipRecruiter's ``robots.txt`` is deny-by-default. For any client that is not on
its named-bot list - which this one is not - the whole site is closed::

    # DEFAULT
    # Deny-by-default, except the LLM manifest.
    User-agent: *
    Allow: /llms.txt
    Disallow: /

So this adapter reads **no** ZipRecruiter web page. Every HTML surface was
probed once during Phase 9B and answers ``403`` behind a Cloudflare challenge
anyway, including the ``Accept: text/markdown`` variant the manifest describes
for named bots. None of that is worked around.

The one path ``User-agent: *`` is allowed to read is ``/llms.txt``, and that
manifest publishes an interface intended for exactly this kind of client::

    ## ZipRecruiter Job Search MCP
    - transport: streamable-http
    - endpoint: https://api.ziprecruiter.com/mcp
    ### Tools
    - search_jobs
      - Search real ZipRecruiter jobs by keywords, location, salary,
        employment type, and posting recency
      - Read only, no auth required

That endpoint is what this adapter calls, with the honest ``JobFinderBot``
user agent, and it answers it. The tool declares ``readOnlyHint: true`` and
needs no credentials, so nothing here logs in, holds a session, or presents
itself as anything other than what it is.

What this portal can and cannot give
------------------------------------
* **An exact posting age.** ``days_ago`` is published per listing, so the
  freshness windows are applied against a real date rather than a guess. The
  tool also accepts ``max_posted_minutes_ago``, but this adapter does not send
  it - see ``_search_once`` for why that costs six times the requests for no
  gain at this depth.
* **A stable job identity.** The listing URL carries a rotating per-request
  token, but a stable job key is embedded inside it and is recovered here, so
  re-running a search recognises the same vacancy instead of duplicating it.
* **No description, at all.** ``search_jobs`` returns title, company, location,
  work arrangement, salary, benefits and age - and nothing else. There is no
  second tool, no job-detail resource, and the pages that carry posting text
  are the ones robots.txt closes. ``description`` is therefore ``None``.

  This is left as ``None`` deliberately. The matching engine rejects a posting
  it cannot read (``insufficient_description``), and padding the field with
  the metadata above - salary, benefits, employment type - would clear that
  gate with text containing no requirements, no responsibilities and no
  skills. That would not make the match real, it would only stop the pipeline
  from noticing it is not. See ``docs/DECISIONS.md`` D-004.
* **Roughly ten listings per query.** ``offset`` pages in fives but stops
  yielding new records past the second page, whatever ``meta.total`` claims.

This adapter collects and normalises. It does not score, deduplicate, rank or
apply to anything.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

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

BASE_URL = "https://www.ziprecruiter.com"
#: Published in https://www.ziprecruiter.com/llms.txt, which is the one path
#: robots.txt allows this client to read.
MCP_ENDPOINT = "https://api.ziprecruiter.com/mcp"
LLMS_MANIFEST = f"{BASE_URL}/llms.txt"

SEARCH_TOOL = "search_jobs"
_PROTOCOL_VERSION = "2024-11-05"

#: The server answers either plain JSON or a single SSE frame depending on what
#: it feels like; both are accepted by ``PortalHttpClient.post_json``.
_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

#: The endpoint returns five results per call and stops producing new records
#: after the second page, so two pages is the whole of what it will give.
PAGE_SIZE = 5
MAX_PAGES = 2

#: Splits "Full-time · On-site" into commitment and work arrangement.
_JOB_TYPE_SEPARATOR = "·"


class ZipRecruiterPortal(BasePortal):
    """Collects listings from ZipRecruiter's published job-search MCP tool."""

    slug: ClassVar[str] = "ziprecruiter"
    display_name: ClassVar[str] = "ZipRecruiter"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug, policy=HttpPolicy.from_options(self.options)
        )
        self._initialized = False

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # The tool does accept max_posted_minutes_ago, but declaring the
            # capability here is what makes the collector re-fetch for every
            # freshness window. See ``_search_once``: this portal is collected
            # once and filtered locally instead, because it rate-limits.
            supports_date_filter=False,
            # There is no per-job tool or resource to fetch anything further.
            supports_description_fetch=False,
            # `offset` pages, within the narrow depth the endpoint allows.
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
        await self._ensure_initialized()

        moment = datetime.now(UTC)
        seen: set[str] = set()
        jobs: list[RawJob] = []

        for query in request.queries:
            if len(jobs) >= request.limit:
                break

            for page in range(MAX_PAGES):
                if len(jobs) >= request.limit:
                    break

                records = await self._search_once(
                    query,
                    offset=page * PAGE_SIZE,
                    location=request.location,
                    remote_only=request.remote_only,
                )
                if not records:
                    break

                fresh = 0
                for record in records:
                    raw = self._to_raw_job(record, now=moment)
                    if raw is None:
                        continue
                    identity = raw.external_id or raw.source_url
                    if identity in seen:
                        continue
                    seen.add(identity)
                    fresh += 1
                    jobs.append(raw)
                    if len(jobs) >= request.limit:
                        break

                # The endpoint repeats its last page rather than returning an
                # empty one, so a page that is entirely already-seen records is
                # how "no more results" actually presents.
                if fresh == 0:
                    break

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            has_more=False,
            next_cursor=None,
            # Collected unfiltered; the caller applies the window against
            # posted_at, which ``days_ago`` makes exact to the day.
            window_applied_by_portal=False,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        """There is nothing further to fetch.

        The search tool is the only tool the endpoint publishes, and the pages
        that carry posting text are closed by robots.txt.
        """
        return job.description

    async def validate_url(self, url: str) -> bool:
        """Not checked over the wire.

        Every listing URL is on ``www.ziprecruiter.com``, which this client is
        not permitted to request - and the token in it is single-use, so a HEAD
        would spend it. Reporting True here would claim a check that did not
        happen, so this reports False and the URL is treated as unverified.
        """
        return False

    async def health_check(self) -> PortalHealth:
        """Reachability of the published endpoint, via its own handshake."""
        try:
            await self._initialize()
        except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=f"MCP endpoint did not respond: {type(exc).__name__}.",
            )
        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))

    # --- MCP plumbing ------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        if not self._initialized:
            await self._initialize()

    async def _initialize(self) -> None:
        """Perform the MCP handshake the protocol requires before any call."""
        await self._rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "jobfinder", "version": "0.1"},
            },
            request_id=1,
        )
        self._initialized = True

    async def _rpc(
        self, method: str, params: dict[str, Any] | None, *, request_id: int
    ) -> dict[str, Any]:
        """One JSON-RPC call, with the envelope's own errors surfaced."""
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        body = await self._http.post_json(
            MCP_ENDPOINT, payload=payload, headers=_MCP_HEADERS
        )

        error = body.get("error")
        if error:
            message = (
                error.get("message") if isinstance(error, dict) else str(error)
            )
            raise PortalParseError(
                f"ZipRecruiter rejected {method}: {message}.", portal=self.slug
            )

        result = body.get("result")
        if not isinstance(result, dict):
            raise PortalParseError(
                f"ZipRecruiter returned no result object for {method}.",
                portal=self.slug,
            )
        return result

    async def _search_once(
        self,
        query: str,
        *,
        offset: int,
        location: str | None,
        remote_only: bool,
    ) -> list[dict[str, Any]]:
        """Call ``search_jobs`` once and return its raw records.

        No ``max_posted_minutes_ago`` is sent, deliberately. The tool accepts
        one, but sending it means one fetch per freshness window - and because
        this portal rarely fills a quota, the collector escalates through all
        six every time. That was twelve requests per search for the ten or so
        records the endpoint will actually surface, and it earned a 429 in
        live testing. Collecting the unfiltered set once and letting the
        caller filter it by ``posted_at`` costs a sixth of the requests and
        loses nothing at this depth (CLAUDE.md section 15: respect rate
        limits).
        """
        arguments: dict[str, Any] = {"job_role": query}

        if offset:
            arguments["offset"] = offset
        if location:
            arguments["location"] = location
        if remote_only:
            arguments["location_types"] = ["REMOTE"]

        result = await self._rpc(
            "tools/call",
            {"name": SEARCH_TOOL, "arguments": arguments},
            request_id=2,
        )

        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise PortalParseError(
                "ZipRecruiter search returned no structured content.",
                portal=self.slug,
            )

        records = structured.get("results")
        if records is None:
            # No matches is legitimate; a missing key means the shape changed.
            raise PortalParseError(
                "ZipRecruiter search result contained no results key.",
                portal=self.slug,
            )
        if not isinstance(records, list):
            raise PortalParseError(
                "ZipRecruiter search results were not a list.", portal=self.slug
            )

        for warning in structured.get("warnings") or ():
            logger.info(
                "ziprecruiter_search_warning",
                portal=self.slug,
                query=query,
                warning=str(warning),
            )

        return [record for record in records if isinstance(record, dict)]

    # --- Parsing -----------------------------------------------------------

    def _to_raw_job(
        self, record: dict[str, Any], *, now: datetime
    ) -> RawJob | None:
        """Map one ZipRecruiter record onto the shared RawJob contract.

        Returns None when the record lacks the minimum needed to be usable - a
        title and a link. A listing the user cannot open is not a result.
        """
        title = str(record.get("title") or "").strip()
        url = str(record.get("job_redirect_url") or "").strip()

        if not title or not url:
            logger.debug(
                "ziprecruiter_record_skipped",
                portal=self.slug,
                has_title=bool(title),
                has_url=bool(url),
            )
            return None

        commitment, _ = self._job_type_parts(record.get("job_type"))

        return RawJob(
            external_id=stable_job_key(url),
            source=self.slug,
            title=title,
            company=str(record.get("company") or "").strip() or None,
            location=str(record.get("location") or "").strip() or None,
            # The permitted surface publishes no posting text. See the module
            # docstring: this stays None rather than being filled with metadata.
            description=None,
            source_url=url,
            application_url=url,
            posted_at=self._posted_at(record.get("days_ago"), now=now),
            employment_type=commitment,
            raw_payload=dict(record),
        )

    @staticmethod
    def _job_type_parts(value: Any) -> tuple[str | None, str | None]:
        """Split "Full-time · On-site" into commitment and work arrangement.

        The work arrangement is returned for callers that want it, but is not
        placed on ``RawJob``: that contract has no work-mode field, and writing
        it into ``location`` instead would corrupt the location key that
        cross-portal deduplication groups on.
        """
        if not isinstance(value, str) or not value.strip():
            return None, None

        parts = [part.strip() for part in value.split(_JOB_TYPE_SEPARATOR)]
        commitment = parts[0] or None
        arrangement = parts[1] if len(parts) > 1 and parts[1] else None
        return commitment, arrangement

    @staticmethod
    def _posted_at(value: Any, *, now: datetime) -> datetime | None:
        """Convert ZipRecruiter's whole-day age into a timestamp.

        The portal publishes ``days_ago`` and nothing finer, so this is
        day-accurate and no more. A listing without one keeps ``posted_at``
        empty rather than being given a made-up date.
        """
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if value < 0:
            return None
        return now - timedelta(days=float(value))


def _protobuf_fields(data: bytes) -> dict[int, bytes]:
    """Length-delimited protobuf fields, by field number.

    Deliberately partial: it reads the one wire type the token uses and stops
    at anything else, because the goal is to recover one identifier, not to
    implement protobuf.
    """
    fields: dict[int, bytes] = {}
    index = 0
    length = len(data)

    while index < length:
        key = data[index]
        index += 1
        field_number, wire_type = key >> 3, key & 0x07
        if wire_type != 2:
            break

        size = 0
        shift = 0
        while index < length:
            byte = data[index]
            index += 1
            size |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7

        if index + size > length:
            break

        fields.setdefault(field_number, data[index : index + size])
        index += size

    return fields


def stable_job_key(url: str) -> str | None:
    """Recover ZipRecruiter's own job key from a listing URL.

    The URL carries ``match_token``, a base64 protobuf holding the job key and
    a per-request identifier. The second changes on every call, so using the
    URL as the job's identity would make the same vacancy look new on every
    search. The first is stable, and is what is returned here.

    Returns None when the token cannot be read, in which case the caller falls
    back to the URL and the worst case is a duplicate rather than a wrong
    merge.
    """
    _, separator, query = url.partition("match_token=")
    if not separator:
        return None

    token = query.split("&", 1)[0].strip()
    if not token:
        return None

    from urllib.parse import unquote

    token = unquote(token)
    # base64url without padding is what appears in the URL.
    padded = token + "=" * (-len(token) % 4)

    try:
        decoded = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None

    envelope = _protobuf_fields(decoded).get(1)
    if envelope is None:
        return None

    key = _protobuf_fields(envelope).get(1)
    if not key:
        return None

    try:
        return key.decode("ascii")
    except UnicodeDecodeError:
        return None


__all__ = [
    "BASE_URL",
    "MAX_PAGES",
    "MCP_ENDPOINT",
    "PAGE_SIZE",
    "ZipRecruiterPortal",
    "stable_job_key",
]
