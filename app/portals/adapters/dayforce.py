"""Dayforce adapter (https://jobs.dayforcehcm.com/).

Access surface, and how it was found
------------------------------------
Dayforce is an ATS. Its candidate portals are Next.js pages that render nothing
server-side, so the listings live behind the API those pages call::

    POST /api/geo/<clientNamespace>/jobposting/search

That endpoint is public - no account, no API key, no partner agreement - but it
is guarded by an ordinary double-submit CSRF check, which is what makes it look
closed at first contact. The site's own bundle states the rule::

    interceptors.request.use(async e => {
      if (!["POST","PUT","PATCH","DELETE"].includes(e.method.toUpperCase())) return e
      e.headers["X-CSRF-TOKEN"] = await getCsrfToken()
      return e
    })

``getCsrfToken`` reads ``GET /api/auth/csrf``, which is public and unauthenticated.
The token must be sent as ``X-CSRF-TOKEN`` *and* its cookie returned with the
request - token alone is refused - which is why this adapter is the one portal
built with ``keep_cookies=True``.

Nothing here logs in, holds an account, solves a challenge or disguises the
client. ``robots.txt`` on this host carries no ``User-agent`` group and no
``Disallow`` at all, so no path is closed to this client.

Dayforce is an ATS, not a job board
-----------------------------------
The same structural fact as Greenhouse, and it shapes this adapter the same
way: **Dayforce publishes no cross-company search.** Each employer has its own
board, addressed by ``clientNamespace`` and ``jobBoardCode``, and the API
answers per board. ``mydayforce/alljobs`` looks like an aggregate and is not -
it is Dayforce's own corporate board, 133 postings worldwide.

So the boards are **configuration, not code**: ``portal_configs.options["boards"]``
replaces the defaults, and adding an employer needs no deployment.

The honest limitations
----------------------
* **Coverage is exactly the configured board list.** This adapter cannot
  "search Dayforce"; it searches the employers it has been told about.
* **The API does not paginate.** Every paging parameter the client sends -
  ``pageNumber``, ``offset``, ``skip``, ``startIndex``, ``count`` - is ignored:
  the response always reports ``offset: 0`` and returns at most 25 postings,
  while ``maxCount`` reports the true total. Breadth therefore comes from
  querying each board once per same-role title variation, not from paging.
* **The payload carries no employer name.** It is taken from the board's own
  configuration entry, which is a declared fact about the board rather than
  something inferred from a posting.

What it gives in exchange
-------------------------
* **The employer's complete posting text**, inline. No second fetch.
* **A real publication timestamp** (``postingStartTimestampUTC``), so the
  30-day window is decided on the employer's own date.
* **Structured locations** with ISO country codes, so the US rule is applied to
  published data rather than to a parsed string.
* **A stable job id and a stable job URL.**

This adapter collects and normalises. It does not score, deduplicate, rank or
apply to anything.
"""

from __future__ import annotations

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

SITE_BASE = "https://jobs.dayforcehcm.com"
CSRF_URL = f"{SITE_BASE}/api/auth/csrf"

#: The culture the portal is queried in. This product is United States only.
CULTURE_CODE = "en-US"


class DayforceBoard:
    """One employer's Dayforce board.

    ``company`` is configuration rather than something read from a posting:
    the search payload carries no employer name, and the board's owner is the
    one fact about it that is known for certain.
    """

    __slots__ = ("namespace", "board", "company")

    def __init__(self, namespace: str, board: str, company: str | None) -> None:
        self.namespace = namespace
        self.board = board
        self.company = company

    def search_url(self) -> str:
        return f"{SITE_BASE}/api/geo/{self.namespace}/jobposting/search"

    def job_url(self, posting_id: object) -> str:
        """The posting's own page, which is what a user opens and applies on."""
        return (
            f"{SITE_BASE}/{CULTURE_CODE}/{self.namespace}/{self.board}"
            f"/jobs/{posting_id}"
        )

    def as_tuple(self) -> tuple[str, str, str | None]:
        return (self.namespace, self.board, self.company)


#: Employer boards read unless an operator configures otherwise. Every one was
#: confirmed to answer when this adapter was written. Chosen for breadth rather
#: than as a recommendation; override with
#: ``portal_configs.options["boards"]``, a list of
#: ``{"namespace": ..., "board": ..., "company": ...}`` objects.
DEFAULT_BOARDS: tuple[tuple[str, str, str | None], ...] = (
    ("mydayforce", "alljobs", "Dayforce"),
    ("golden1", "candidateportal", "Golden 1 Credit Union"),
    ("sarnova", "candidateportal", "Sarnova"),
    ("otg", "candidateportal", "OTG"),
    ("odc", "candidateportal", "Oil-Dri Corporation of America"),
    ("bwp", "candidateportal", "Boardwalk Pipelines"),
    ("prpa", "candidateportal", "Platte River Power Authority"),
    ("navex", "jobopenings", "NAVEX"),
)

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Single letters match nearly every title and carry no signal.
_MIN_TOKEN_LENGTH = 2


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

    The API's own ``searchText`` is fuzzy and score-ranked: asking it for
    "Network Engineer" returns "DevOps Engineer" and "Senior Cloud Engineer"
    too. That is the portal's behaviour, not a defect, but it means the
    response is a candidate list rather than an answer.

    Every significant word of the query must appear in the title, so "Financial
    Analyst" reaches "Senior Financial Analyst" but not "Business Analyst".
    Deliberately blunt: the role gate downstream is the authority on relevance
    (CLAUDE.md section 7).
    """
    wanted = title_tokens(query)
    return bool(wanted) and wanted <= title_tokens(title)


def format_location(posting: dict[str, Any]) -> str | None:
    """The posting's location as the employer published it.

    Prefers a United States entry when the posting lists several, because this
    product returns US vacancies only and the US location is the one that
    matters. The verdict itself is still made downstream by
    ``app.processing.us_locations`` against this string - nothing here decides
    eligibility.
    """
    locations = posting.get("postingLocations")
    if not isinstance(locations, list):
        return None

    entries = [entry for entry in locations if isinstance(entry, dict)]
    if not entries:
        return None

    preferred = next(
        (entry for entry in entries if entry.get("isoCountryCode") == "US"),
        entries[0],
    )

    formatted = str(preferred.get("formattedAddress") or "").strip()
    if formatted:
        return formatted

    # Build from the parts when the address was not pre-formatted, rather than
    # returning nothing and losing an otherwise usable posting.
    parts = [
        str(preferred.get(field) or "").strip()
        for field in ("cityName", "stateCode", "isoCountryCode")
    ]
    joined = ", ".join(part for part in parts if part)
    return joined or None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse Dayforce's ISO-8601 posting timestamp.

    A posting without a usable one keeps ``posted_at`` empty rather than being
    given a made-up date; the freshness gate then excludes it, which is the
    behaviour CLAUDE.md section 6 requires for an unverifiable date.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class DayforcePortal(BasePortal):
    """Collects listings from Dayforce's public candidate-portal search API."""

    slug: ClassVar[str] = "dayforce"
    display_name: ClassVar[str] = "Dayforce"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug,
            policy=HttpPolicy.from_options(self.options),
            # The CSRF cookie has to come back with the POST that carries the
            # token. See the module docstring.
            keep_cookies=True,
        )
        self._boards = self._configured_boards(self.options)
        #: Fetched once per adapter, which is one search run.
        self._csrf_token: str | None = None

    @staticmethod
    def _configured_boards(options: dict[str, Any]) -> tuple[DayforceBoard, ...]:
        """Boards from configuration, falling back to the defaults.

        Accepts the mapping form an operator would write. A malformed entry is
        skipped rather than failing the portal, so one bad row in a config blob
        cannot take the whole adapter down.
        """
        configured = options.get("boards")
        boards: list[DayforceBoard] = []

        if isinstance(configured, list | tuple):
            for entry in configured:
                if isinstance(entry, dict):
                    namespace = str(entry.get("namespace") or "").strip()
                    board = str(entry.get("board") or "").strip()
                    company = entry.get("company")
                elif isinstance(entry, list | tuple) and len(entry) >= 2:
                    namespace = str(entry[0] or "").strip()
                    board = str(entry[1] or "").strip()
                    company = entry[2] if len(entry) > 2 else None
                else:
                    continue

                if not namespace or not board:
                    continue
                cleaned = str(company).strip() if company else None
                boards.append(DayforceBoard(namespace, board, cleaned or None))

        if boards:
            return tuple(boards)
        return tuple(
            DayforceBoard(namespace, board, company)
            for namespace, board, company in DEFAULT_BOARDS
        )

    @property
    def boards(self) -> tuple[DayforceBoard, ...]:
        return self._boards

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # The API exposes no usable date filter; freshness is applied by the
            # caller against posted_at, which the posting timestamp makes exact.
            supports_date_filter=False,
            # The full posting text arrives with the listing.
            supports_description_fetch=False,
            # Verified absent: every paging parameter is ignored and the
            # response always reports offset 0. See the module docstring.
            supports_pagination=False,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings matching the request's same-role title variations.

        Boards are queried independently: one board being unavailable costs
        that employer's listings, not the whole pass.
        """
        seen: set[str] = set()
        jobs: list[RawJob] = []
        failures: list[str] = []

        # Obtained once, before any board is queried. A session that cannot be
        # established is a failure of the portal, not of one employer's board:
        # letting it surface here reports it as what it is, instead of being
        # caught per board and reported as "every board was unavailable".
        await self._csrf()

        for board in self._boards:
            if len(jobs) >= request.limit:
                break

            for query in request.queries:
                if len(jobs) >= request.limit:
                    break

                try:
                    postings = await self._search_board(board, query)
                except PortalError as exc:
                    logger.info(
                        "dayforce_board_unavailable",
                        portal=self.slug,
                        board=f"{board.namespace}/{board.board}",
                        error=exc.message,
                    )
                    failures.append(board.namespace)
                    break

                for posting in postings:
                    if len(jobs) >= request.limit:
                        break
                    if not title_matches(query, str(posting.get("jobTitle") or "")):
                        continue

                    raw = self._to_raw_job(posting, board=board)
                    if raw is None:
                        continue
                    identity = raw.external_id or raw.source_url
                    if identity in seen:
                        continue
                    seen.add(identity)
                    jobs.append(raw)

        if failures and len(set(failures)) == len(self._boards):
            # Every board failed: that is the portal being down, and the run
            # must record it rather than reporting "no matching jobs".
            raise PortalError(
                f"No Dayforce board could be read ({len(set(failures))} attempted).",
                portal=self.slug,
            )

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            # The API has no paging, so one pass is the whole of what it will
            # give for this query. Saying otherwise would make the collector
            # ask for a page that does not exist.
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
        try:
            await self._search_board(self._boards[0], "")
        except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=f"Search API did not respond: {type(exc).__name__}.",
            )
        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))

    # --- Fetching ----------------------------------------------------------

    async def _csrf(self) -> str:
        """The CSRF token this session's writes must carry.

        Fetched once per adapter. The matching cookie is retained by the HTTP
        client, so the token and its cookie stay together as the portal
        requires.
        """
        if self._csrf_token is not None:
            return self._csrf_token

        payload = await self._http.get_json(CSRF_URL)
        token = payload.get("csrfToken")
        if not isinstance(token, str) or not token:
            raise PortalParseError(
                "Dayforce did not issue a CSRF token.", portal=self.slug
            )

        self._csrf_token = token
        return token

    async def _search_board(
        self, board: DayforceBoard, query: str
    ) -> list[dict[str, Any]]:
        """One board's postings for one query."""
        token = await self._csrf()

        payload = await self._http.post_json(
            board.search_url(),
            payload={
                "clientNamespace": board.namespace,
                "jobBoardCode": board.board,
                "cultureCode": CULTURE_CODE,
                "searchText": query,
            },
            headers={
                "X-CSRF-TOKEN": token,
                "Content-Type": "application/json",
                "Origin": SITE_BASE,
                "Referer": f"{SITE_BASE}/{CULTURE_CODE}/{board.namespace}/{board.board}",
            },
        )

        postings = payload.get("jobPostings")
        if postings is None:
            # A board with no openings still returns the key; its absence means
            # the shape changed and should be surfaced.
            raise PortalParseError(
                f"Dayforce board {board.namespace} returned no jobPostings key.",
                portal=self.slug,
            )
        if not isinstance(postings, list):
            raise PortalParseError(
                f"Dayforce board {board.namespace} returned a non-list of postings.",
                portal=self.slug,
            )

        clean = [posting for posting in postings if isinstance(posting, dict)]
        logger.info(
            "dayforce_board_searched",
            portal=self.slug,
            board=f"{board.namespace}/{board.board}",
            query=query,
            returned=len(clean),
            max_count=payload.get("maxCount"),
        )
        return clean

    # --- Parsing -----------------------------------------------------------

    def _to_raw_job(
        self, posting: dict[str, Any], *, board: DayforceBoard
    ) -> RawJob | None:
        """Map one Dayforce posting onto the shared RawJob contract.

        Returns None when the posting lacks the minimum needed to be usable -
        an id and a title. A listing the user cannot open is not a result.
        """
        posting_id = posting.get("jobPostingId")
        title = str(posting.get("jobTitle") or "").strip()

        if posting_id in (None, "") or not title:
            logger.debug(
                "dayforce_posting_skipped",
                portal=self.slug,
                board=board.namespace,
                has_id=posting_id not in (None, ""),
                has_title=bool(title),
            )
            return None

        url = board.job_url(posting_id)
        description = str(posting.get("jobDescription") or "").strip() or None

        return RawJob(
            # Namespaced, so two employers' posting 42 stay distinct.
            external_id=f"{board.namespace}:{posting_id}",
            source=self.slug,
            title=title,
            # From the board's configuration: the payload names no employer,
            # and the board's owner is the one thing known for certain.
            company=board.company,
            location=format_location(posting),
            description=description,
            source_url=url,
            application_url=url,
            posted_at=parse_timestamp(posting.get("postingStartTimestampUTC")),
            # Dayforce publishes no employment-type field on the search payload.
            employment_type=None,
            # Dayforce is not a board that links out to an employer's
            # application - it *is* the employer's application system, and the
            # posting page is where the form lives. A statement about the
            # surface, not a guess about one listing.
            raw_payload=declare_application_type(
                dict(posting), ApplicationType.LONG_APPLICATION
            ),
        )


__all__ = [
    "CSRF_URL",
    "CULTURE_CODE",
    "DEFAULT_BOARDS",
    "SITE_BASE",
    "DayforceBoard",
    "DayforcePortal",
    "format_location",
    "parse_timestamp",
    "title_matches",
    "title_tokens",
]
