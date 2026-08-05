"""Greenhouse adapter (https://boards-api.greenhouse.io/).

Access surface, and why this one
--------------------------------
Greenhouse publishes a documented, public, no-auth **Job Board API**, and its
``robots.txt`` closes almost nothing::

    User-agent: *
    Disallow: /embed/

``/v1/boards/<token>/jobs`` is not on that list, and it is the endpoint this
adapter uses. Nothing here logs in, holds a session, or touches ``/embed/``.

Greenhouse is an ATS, not a job board
-------------------------------------
This is the structural difference from every portal added so far, and it
shapes the whole adapter: **Greenhouse publishes no cross-company search.**
Each employer has its own board, addressed by a board token, and the API
answers per board. There is no endpoint that searches all of them.

The job-seeker site at ``my.greenhouse.io`` does have a search box, but it is
client-rendered - the page is a 5.8 KB shell that returns identical markup for
every query - so reading it would need browser automation, which this project
does not do.

So collection works the other way round from a board: the configured boards
are fetched, and the search queries filter them by title. The boards are
**configuration, not code** - ``portal_configs.options["board_tokens"]``
overrides the default list, so adding an employer needs no deployment.

The honest limitation that follows
----------------------------------
Coverage is exactly the configured board list and no wider. This portal cannot
"search Greenhouse"; it searches the employers it has been told about. That is
a property of what Greenhouse publishes, not of this implementation, and it is
recorded in the portal's seed notes so it is visible in the dashboard.

What it gives in exchange
-------------------------
* **The employer's complete posting text**, in one request per board. No
  second fetch, no composed summary, no truncation.
* **Employer-direct application URLs.** ``absolute_url`` is the employer's own
  careers page for boards that host one (``stripe.com/jobs/...``), which the
  URL preference rules rank above any board listing.
* **A stable job identity** and a real publication timestamp.

This adapter collects and normalises. It does not score, deduplicate, rank or
apply to anything.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
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
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
)
from app.portals.http import HttpPolicy, PortalHttpClient

logger = get_logger(__name__)

API_BASE = "https://boards-api.greenhouse.io/v1/boards"

#: Employers whose public Greenhouse boards are read unless an operator
#: configures otherwise. Chosen for breadth rather than as a recommendation,
#: and every one was confirmed to resolve when this adapter was written.
#: Override with ``portal_configs.options["board_tokens"]``.
DEFAULT_BOARD_TOKENS: tuple[str, ...] = (
    "stripe",
    "databricks",
    "brex",
    "affirm",
    "instacart",
    "chime",
    "flexport",
    "robinhood",
)

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[a-z0-9]+")

#: Single letters match nearly every title and carry no signal: "FP&A Analyst"
#: must require "fp" and "analyst", not "a".
_MIN_TOKEN_LENGTH = 2

#: Metadata field names that carry a commitment, when a board defines one.
#: Greenhouse has no standard employment-type field.
_EMPLOYMENT_FIELDS = ("employment type", "job type", "employment status")


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
    """Whether a board title answers a search query.

    Every significant word of the query must appear in the title, so
    "Financial Analyst" reaches "Senior Financial Analyst" and "Financial
    Analyst, Business F&S" but not "Business Analyst".

    Deliberately blunt: this is the search Greenhouse does not provide, not a
    scoring step. The role gate downstream is what actually decides relevance,
    and it is the authority (CLAUDE.md section 7).
    """
    wanted = title_tokens(query)
    return bool(wanted) and wanted <= title_tokens(title)


def html_to_text(value: Any) -> str | None:
    """Flatten a Greenhouse posting into readable plain text.

    ``content`` arrives HTML-escaped inside the JSON, so it is unescaped once
    to recover the markup, then flattened. Block boundaries become newlines so
    headings and list items do not run together - the matching layer reads
    those headings to tell a stated requirement from prose.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    markup = html.unescape(value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", markup)
    text = re.sub(r"(?i)</(p|div|li|ul|ol|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RUN_RE.sub("\n\n", text).strip()

    return text or None


class GreenhousePortal(BasePortal):
    """Collects listings from the public Greenhouse Job Board API."""

    slug: ClassVar[str] = "greenhouse"
    display_name: ClassVar[str] = "Greenhouse"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug, policy=HttpPolicy.from_options(self.options)
        )
        self._boards = self._configured_boards(self.options)
        #: Board payloads already fetched during this run. Every query filters
        #: the same boards, so without this each same-role variation would
        #: re-download every board.
        self._cache: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _configured_boards(options: dict[str, Any]) -> tuple[str, ...]:
        """Board tokens from configuration, falling back to the defaults."""
        configured = options.get("board_tokens")
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, list | tuple):
            tokens = tuple(
                str(token).strip() for token in configured if str(token).strip()
            )
            if tokens:
                return tokens
        return DEFAULT_BOARD_TOKENS

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # The API exposes no date filter; freshness is applied by the
            # caller against posted_at, which first_published makes exact.
            supports_date_filter=False,
            # The full posting text arrives with the listing, so there is
            # nothing left to fetch.
            supports_description_fetch=False,
            # One request returns a board in full; there are no pages.
            supports_pagination=False,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings matching the request's same-role title variations.

        Boards are fetched independently: one board being unavailable costs
        that employer's listings, not the whole pass.
        """
        seen: set[str] = set()
        jobs: list[RawJob] = []
        failures: list[str] = []

        for token in self._boards:
            if len(jobs) >= request.limit:
                break

            try:
                records = await self._board(token)
            except (PortalBlockedError, PortalRateLimitedError):
                # A refusal is respected immediately and for the whole portal:
                # moving on to the next board would be ignoring what the API
                # just said.
                raise
            except PortalError as exc:
                logger.info(
                    "greenhouse_board_unavailable",
                    portal=self.slug,
                    board=token,
                    error=exc.message,
                )
                failures.append(token)
                continue

            for record in records:
                if len(jobs) >= request.limit:
                    break
                if not any(
                    title_matches(query, str(record.get("title") or ""))
                    for query in request.queries
                ):
                    continue

                raw = self._to_raw_job(record, board=token)
                if raw is None:
                    continue
                identity = raw.external_id or raw.source_url
                if identity in seen:
                    continue
                seen.add(identity)
                jobs.append(raw)

        if failures and len(failures) == len(self._boards):
            # Every board failed: that is the portal being down, and the run
            # must record it rather than reporting "no matching jobs".
            raise PortalError(
                f"No Greenhouse board could be read ({len(failures)} attempted).",
                portal=self.slug,
            )

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
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
        """Reachability of the API, via the first configured board."""
        token = self._boards[0] if self._boards else DEFAULT_BOARD_TOKENS[0]
        try:
            await self._board(token)
        except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=f"Board API did not respond: {type(exc).__name__}.",
            )
        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))

    # --- Fetching ----------------------------------------------------------

    async def _board(self, token: str) -> list[dict[str, Any]]:
        """One employer's published jobs, with their full posting text.

        Cached for the life of this adapter, which is one search run: the same
        boards answer every same-role query, and re-downloading them per query
        would multiply the request count by the number of title variations.
        """
        if token in self._cache:
            return self._cache[token]

        body = await self._http.get_text(
            f"{API_BASE}/{token}/jobs", params={"content": "true"}
        )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PortalParseError(
                f"Greenhouse board {token} returned invalid JSON: {exc.msg}.",
                portal=self.slug,
            ) from exc

        if not isinstance(payload, dict):
            raise PortalParseError(
                f"Greenhouse board {token} returned "
                f"{type(payload).__name__}, expected an object.",
                portal=self.slug,
            )

        records = payload.get("jobs")
        if records is None:
            # A board with no openings is legitimate; a payload with no jobs
            # key at all means the shape changed and should be surfaced.
            raise PortalParseError(
                f"Greenhouse board {token} returned no jobs key.", portal=self.slug
            )
        if not isinstance(records, list):
            raise PortalParseError(
                f"Greenhouse board {token} returned a non-list of jobs.",
                portal=self.slug,
            )

        clean = [record for record in records if isinstance(record, dict)]
        self._cache[token] = clean
        logger.info(
            "greenhouse_board_loaded",
            portal=self.slug,
            board=token,
            count=len(clean),
        )
        return clean

    # --- Parsing -----------------------------------------------------------

    def _to_raw_job(
        self, record: dict[str, Any], *, board: str
    ) -> RawJob | None:
        """Map one Greenhouse posting onto the shared RawJob contract.

        Returns None when the posting lacks the minimum needed to be usable -
        an id, a title and a link. A listing the user cannot open is not a
        result.
        """
        job_id = record.get("id")
        title = str(record.get("title") or "").strip()
        url = str(record.get("absolute_url") or "").strip()

        if not job_id or not title or not url:
            logger.debug(
                "greenhouse_record_skipped",
                portal=self.slug,
                board=board,
                has_id=bool(job_id),
                has_title=bool(title),
                has_url=bool(url),
            )
            return None

        return RawJob(
            external_id=str(job_id),
            source=self.slug,
            title=title,
            # Greenhouse reports the employer on the posting itself; the board
            # token is only how the board was addressed.
            company=str(record.get("company_name") or "").strip() or board,
            location=self._location(record),
            description=html_to_text(record.get("content")),
            source_url=url,
            application_url=url,
            posted_at=self._posted_at(
                record.get("first_published") or record.get("updated_at")
            ),
            employment_type=self._employment_type(record),
            raw_payload=dict(record),
        )

    @staticmethod
    def _location(record: dict[str, Any]) -> str | None:
        """The posting's stated location, or the first office it names."""
        location = record.get("location")
        if isinstance(location, dict):
            name = str(location.get("name") or "").strip()
            if name:
                return name

        offices = record.get("offices")
        if isinstance(offices, list):
            for office in offices:
                if isinstance(office, dict):
                    name = str(office.get("name") or "").strip()
                    if name:
                        return name

        return None

    @classmethod
    def _employment_type(cls, record: dict[str, Any]) -> str | None:
        """A commitment, when the board happens to publish one.

        Greenhouse has no standard field for this, so it is read from the
        board's own custom metadata when present and left empty otherwise
        rather than assumed to be full time.
        """
        metadata = record.get("metadata")
        if not isinstance(metadata, list):
            return None

        for field in metadata:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "").casefold().strip()
            if name not in _EMPLOYMENT_FIELDS:
                continue
            value = field.get("value")
            if isinstance(value, list):
                value = value[0] if value else None
            if isinstance(value, str) and value.strip():
                return value.strip()

        return None

    @staticmethod
    def _posted_at(value: Any) -> datetime | None:
        """Parse Greenhouse's ISO-8601 publication time.

        A posting without one keeps ``posted_at`` empty rather than being
        given a made-up date.
        """
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "API_BASE",
    "DEFAULT_BOARD_TOKENS",
    "GreenhousePortal",
    "html_to_text",
    "title_matches",
    "title_tokens",
]
