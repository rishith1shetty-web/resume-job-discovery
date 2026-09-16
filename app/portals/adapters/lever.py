"""Lever adapter (https://api.lever.co/v0/postings/).

Access surface, and why this one
--------------------------------
Lever publishes a public, no-auth postings feed per employer board::

    GET https://api.lever.co/v0/postings/<board>?mode=json

``api.lever.co/robots.txt`` closes nothing::

    User-agent: *
    Allow: /
    Crawl-delay: 1

Nothing here logs in, holds a session, or sends a cookie. The authenticated
``/v1/postings`` API exists and answers ``401 UnauthorizedError``; it is
deliberately not used.

``jobs.lever.co`` - which serves the job and apply pages this adapter links to
- allows ``*`` as well, and blanket-disallows a named list of AI crawlers
(``GPTBot``, ``CCBot``, ``ClaudeBot`` and others). This client's product token
is ``jobfinderbot``, which is not on that list, so it falls under the wildcard
group. Its ``Content-Signal`` is ``search=yes,ai-train=no,use=reference``:
this project builds a search result and links back, and it trains nothing -
``Settings.enable_llm_assist`` is False and its validator refuses to start the
application if it is turned on.

Lever is an ATS, not a job board
--------------------------------
The same structural fact that shapes ``greenhouse.py`` and ``dayforce.py``:
**Lever publishes no cross-company search.** Each employer has its own board
and the feed answers per board. ``jobs.lever.co/`` redirects to the marketing
site, ``/search?q=`` is a 404, and there is no board index, sitemap or company
directory to enumerate.

So collection works the other way round from a board: the configured boards are
fetched, and the search queries filter them by title. The boards are
**configuration, not code** - ``portal_configs.options["boards"]`` overrides the
default list, so adding an employer needs no deployment.

Two query parameters look useful and are not. ``?query=`` and ``?search=`` are
**silently ignored** - they return the whole board, unfiltered - and so is
``?workplaceType=``. Measured on a 781-posting board: every one of those
parameters returned all 781 records. Sending them would produce a filter that
appears to work and does not, so this adapter sends neither. ``limit``/``skip``
*do* paginate, but one request already returns the board in full, so they are
not used either.

The honest limitation that follows
----------------------------------
Coverage is exactly the configured board list and no wider. This portal cannot
"search Lever"; it searches the employers it has been told about. That is a
property of what Lever publishes, not of this implementation, and it is
recorded in the portal's seed notes so it is visible in the dashboard.

What it gives in exchange
-------------------------
* **The employer's complete posting text**, in one request per board, already
  split into named sections - ``Responsibilities``, ``Minimum Qualifications``
  and so on - which the matching layer reads to tell a stated requirement from
  prose. No second fetch and no truncation.
* **An ISO-3166 country code on every posting**, so the United States rule is
  applied to a published fact rather than to a guessed string.
* **A stable UUID identity**, employer-direct apply URLs, and a real
  publication timestamp.

The one field Lever does not publish is the **employer's name**. It is not in
the feed at all, so it comes from the board's configuration entry - the same
solution ``dayforce.py`` uses for the same gap, and the reason a board may not
be added without naming its owner.

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
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
)
from app.portals.http import HttpPolicy, PortalHttpClient
from app.processing.us_locations import classify_us_location

logger = get_logger(__name__)

API_BASE = "https://api.lever.co/v0/postings"


class LeverBoard:
    """One employer's Lever board.

    ``company`` is configuration rather than something read from a posting: the
    feed carries no employer name anywhere, and the board's owner is the one
    fact about it that is known for certain. Deriving it from the slug would be
    a guess dressed as data - ``matchgroup`` is "Match Group" and ``fly`` is
    "Fly.io", neither of which any rule would produce.
    """

    __slots__ = ("slug", "company")

    def __init__(self, slug: str, company: str | None) -> None:
        self.slug = slug
        self.company = company

    def feed_url(self) -> str:
        return f"{API_BASE}/{self.slug}"

    def as_tuple(self) -> tuple[str, str | None]:
        return (self.slug, self.company)


#: Employer boards read unless an operator configures otherwise. Override with
#: ``portal_configs.options["boards"]``, a list of ``{"slug": ..., "company":
#: ...}`` objects.
#:
#: Every entry here was proved live rather than inferred: each slug was
#: confirmed to answer ``200`` with a JSON array on ``/v0/postings/<slug>``,
#: *and* its employer name was read from that board's own
#: ``schema.org/JobPosting`` ``hiringOrganization.name`` on a real job page.
#: The name is therefore the employer's own statement of who they are.
#:
#: A slug is a hypothesis, not a name. Measured across 88 plausible guesses
#: derived from well-known company names, 11 resolved - 12.5%. Netflix, Figma,
#: Notion, Rippling, Reddit and Stripe all 404. That is why nothing may be
#: added here without being fetched first.
#:
#: Four further boards answered ``200`` and are deliberately **absent**:
#: ``cred`` (11 postings, none in the United States), and ``kraken``,
#: ``wealthsimple`` and ``brightwheel``, which are all currently empty. An
#: empty board publishes no job page, so its employer name could only have been
#: guessed - and this list does not guess.
#:
#: Per-board yield is low, as it is for every ATS: measured across these
#: boards, 1,539 postings held 1,050 United States roles, of which 37 were
#: inside the 30-day window. Breadth comes from the number of employers rather
#: than from any single one, and a board is a standing subscription to an
#: employer rather than a snapshot of one day's postings.
DEFAULT_BOARDS: tuple[tuple[str, str], ...] = (
    ("gopuff", "Gopuff"),
    ("palantir", "Palantir Technologies"),
    ("spotify", "Spotify"),
    ("matchgroup", "Match Group"),
    ("binance", "Binance"),
    ("fly", "Fly.io"),
)

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[a-z0-9]+")

#: Single letters match nearly every title and carry no signal: "FP&A Analyst"
#: must require "fp" and "analyst", not "a".
_MIN_TOKEN_LENGTH = 2

#: Lever's own vocabulary for how a role is worked. Surfaced into the
#: description as a labelled line so the shared ``detect_work_mode`` reads it -
#: ``RawJob`` has no work-mode field, and inventing one would change the
#: adapter contract for every portal.
_WORKPLACE_TYPES = frozenset({"remote", "hybrid", "onsite"})


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

    Deliberately blunt: this is the search Lever does not provide, not a
    scoring step. The role gate downstream is what actually decides relevance,
    and it is the authority (CLAUDE.md section 7).

    Character-for-character the rule ``greenhouse.py``, ``dayforce.py`` and
    ``google_careers.py`` each state for themselves, and stated here for the
    same reason they do: no adapter in this package imports another, so a
    portal can be changed or retired without disturbing the rest.
    ``tests/test_lever_adapter.py`` pins this to Greenhouse's answer over a
    shared table of cases, so the copies cannot drift apart unnoticed.
    """
    wanted = title_tokens(query)
    return bool(wanted) and wanted <= title_tokens(title)


def html_to_text(value: Any) -> str | None:
    """Flatten one of Lever's HTML fragments into readable plain text.

    Used for ``lists[].content``, which is the only part of a posting Lever
    publishes as markup with no plain-text twin. Block boundaries become
    newlines and list items gain a bullet, so requirements do not run together
    into one line.
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


def build_description(record: dict[str, Any]) -> str | None:
    """The employer's full posting text, assembled without repeating itself.

    Lever publishes the same prose several times over, and the fields nest
    rather than sit side by side. Measured on live boards:

    * ``openingPlain`` is contained in ``descriptionPlain`` on 307 of the 307
      Palantir postings that carry one;
    * ``descriptionPlain`` and ``descriptionBodyPlain`` are identical on 70 of
      71 Spotify postings.

    So ``descriptionPlain`` is the widest of the three, and taking any of the
    others alongside it would feed the scorer the same paragraphs twice.
    ``lists`` and ``additionalPlain`` are genuinely separate text and are both
    kept.

    Section headings are preserved deliberately. ``lists[].text`` carries names
    like "Responsibilities" and "Minimum Qualifications" pre-separated from
    their content, which is better structured than the single markup blob most
    boards serve, and the matching layer reads exactly those headings.
    """
    parts: list[str] = []

    # `descriptionPlain` when present, else the HTML twin flattened. Only one
    # of them, and never `descriptionBodyPlain` or `openingPlain` as well.
    lead = record.get("descriptionPlain")
    if not isinstance(lead, str) or not lead.strip():
        lead = html_to_text(record.get("description"))
    if isinstance(lead, str) and lead.strip():
        parts.append(lead.strip())

    for section in record.get("lists") or []:
        if not isinstance(section, dict):
            continue
        body = html_to_text(section.get("content"))
        if not body:
            continue
        heading = str(section.get("text") or "").strip()
        parts.append(f"{heading}\n{body}" if heading else body)

    closing = record.get("additionalPlain")
    if not isinstance(closing, str) or not closing.strip():
        closing = html_to_text(record.get("additional"))
    if isinstance(closing, str) and closing.strip():
        parts.append(closing.strip())

    # How the role is worked, when Lever published it. `RawJob` carries no
    # field for this, so it goes into the text the shared work-mode detector
    # already reads. Only Lever's own vocabulary is emitted - an unrecognised
    # value is dropped rather than guessed at.
    workplace = str(record.get("workplaceType") or "").strip().casefold()
    if workplace in _WORKPLACE_TYPES:
        parts.insert(0, f"Workplace type: {workplace.capitalize()}")

    text = "\n\n".join(parts).strip()
    return text or None


def format_location(record: dict[str, Any]) -> str | None:
    """The posting's location as the employer published it.

    Prefers a United States entry when a United-States-coded posting lists
    several, because this product returns US vacancies only and the US location
    is the one that matters - the same preference ``dayforce.py`` and
    ``google_careers.py`` apply to their own multi-location postings.

    The country code gates that preference. A posting Lever codes ``GB`` keeps
    its published location even if one of its other offices reads like a US
    city, because the alternative is inferring a country the employer did not
    claim. Measured across 1,236 live postings, no non-US-coded posting had a
    US secondary location, so the gate costs nothing.

    The verdict itself is still made downstream by
    ``app.processing.us_locations`` against this string; nothing here decides
    eligibility.
    """
    categories = record.get("categories")
    categories = categories if isinstance(categories, dict) else {}

    primary = str(categories.get("location") or "").strip()
    country = str(record.get("country") or "").strip().upper()

    if country == "US":
        for entry in categories.get("allLocations") or []:
            if not isinstance(entry, str) or not entry.strip():
                continue
            candidate = entry.strip()
            if classify_us_location(candidate).is_us:
                return candidate

    return primary or None


def parse_created_at(value: Any) -> datetime | None:
    """Lever's ``createdAt``, in epoch milliseconds, as an aware UTC datetime.

    ``createdAt`` is the **only** timestamp the public feed carries - there is
    no ``updatedAt``, refresh or repost field to confuse it with - and it is
    the original publication moment. Verified against Lever's own
    ``schema.org/JobPosting`` ``datePosted`` on eight postings across four
    employers, ages spanning 0 to 6,127 days: all eight agreed exactly, and a
    posting created in December 2009 still declares ``2009-12-05``. Lever does
    not restamp it.

    A posting whose timestamp is missing, non-numeric or out of range keeps
    ``posted_at`` empty rather than being given a substitute. The freshness gate
    then excludes it, which is what CLAUDE.md section 6 requires of a date that
    cannot be verified - and is why no other field is consulted here.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None

    try:
        moment = datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        # Far-future or far-past values are not dates this product can use.
        return None

    return moment


class LeverPortal(BasePortal):
    """Collects listings from the public Lever postings feed."""

    slug: ClassVar[str] = "lever"
    display_name: ClassVar[str] = "Lever"

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
    def _configured_boards(options: dict[str, Any]) -> tuple[LeverBoard, ...]:
        """Boards from configuration, falling back to the defaults.

        Accepts the mapping form an operator would write, and the pair form.
        A malformed entry is skipped rather than failing the portal, so one bad
        row in a config blob cannot take the whole adapter down.

        An entry with no company name is still accepted: the employer is then
        simply unknown for those listings, which is honest, where inventing one
        from the slug would not be.
        """
        configured = options.get("boards")
        boards: list[LeverBoard] = []

        if isinstance(configured, list | tuple):
            for entry in configured:
                if isinstance(entry, dict):
                    slug = str(entry.get("slug") or "").strip()
                    company = entry.get("company")
                elif isinstance(entry, str):
                    slug, company = entry.strip(), None
                elif isinstance(entry, list | tuple) and entry:
                    slug = str(entry[0] or "").strip()
                    company = entry[1] if len(entry) > 1 else None
                else:
                    continue

                if not slug:
                    continue
                cleaned = str(company).strip() if company else None
                boards.append(LeverBoard(slug, cleaned or None))

        if boards:
            return tuple(boards)
        return tuple(LeverBoard(slug, company) for slug, company in DEFAULT_BOARDS)

    @property
    def boards(self) -> tuple[LeverBoard, ...]:
        return self._boards

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # The feed exposes no date filter; freshness is applied by the
            # caller against posted_at, which createdAt makes exact.
            supports_date_filter=False,
            # The full posting text arrives with the listing, so there is
            # nothing left to fetch.
            supports_description_fetch=False,
            # `limit`/`skip` exist and work, but one request returns the board
            # in full, so this adapter never pages and declares that plainly
            # rather than advertising a capability it does not exercise.
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
        truncated = False

        for board in self._boards:
            if len(jobs) >= request.limit:
                break

            if out_of_time(request):
                # The caller is out of time. Returning what is already here
                # keeps it; being cancelled mid-loop would not.
                truncated = True
                break

            try:
                records = await self._board(board)
            except (PortalBlockedError, PortalRateLimitedError):
                # A refusal is respected immediately and for the whole portal:
                # moving on to the next board would be ignoring what the API
                # just said.
                raise
            except PortalError as exc:
                logger.info(
                    "lever_board_unavailable",
                    portal=self.slug,
                    board=board.slug,
                    error=exc.message,
                )
                failures.append(board.slug)
                continue

            for record in records:
                if len(jobs) >= request.limit:
                    break
                if not any(
                    title_matches(query, str(record.get("text") or ""))
                    for query in request.queries
                ):
                    continue

                raw = self._to_raw_job(record, board=board)
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
                f"No Lever board could be read ({len(failures)} attempted).",
                portal=self.slug,
            )

        return PortalSearchResult(
            jobs=jobs,
            window=request.window,
            has_more=False,
            next_cursor=None,
            # Collected unfiltered; the caller applies the freshness window.
            window_applied_by_portal=False,
            # True only when a deadline stopped the sweep early. The jobs
            # above are complete and valid; the sweep is not.
            partial=truncated,
        )

    async def fetch_description(self, job: RawJob) -> str | None:
        """The full posting text already arrived with the listing."""
        return job.description

    async def validate_url(self, url: str) -> bool:
        return await self._http.head_ok(url)

    async def health_check(self) -> PortalHealth:
        """Reachability of the feed, via the first configured board."""
        board = (
            self._boards[0]
            if self._boards
            else LeverBoard(*DEFAULT_BOARDS[0])
        )
        try:
            await self._board(board)
        except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=f"Postings feed did not respond: {type(exc).__name__}.",
            )
        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))

    # --- Fetching ----------------------------------------------------------

    async def _board(self, board: LeverBoard) -> list[dict[str, Any]]:
        """One employer's published postings, with their full text.

        Cached for the life of this adapter, which is one search run: the same
        boards answer every same-role query, and re-downloading them per query
        would multiply the request count by the number of title variations.

        ``mode=json`` is the only parameter sent. No keyword, workplace-type or
        paging parameter goes out - see the module docstring for what those do
        and do not do.
        """
        if board.slug in self._cache:
            return self._cache[board.slug]

        body = await self._http.get_text(board.feed_url(), params={"mode": "json"})

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PortalParseError(
                f"Lever board {board.slug} returned invalid JSON: {exc.msg}.",
                portal=self.slug,
            ) from exc

        if not isinstance(payload, list):
            # A board with no openings answers `200 []`, which is a list and is
            # legitimately empty. Anything that is not a list means the shape
            # changed and should be surfaced rather than read as "no jobs".
            raise PortalParseError(
                f"Lever board {board.slug} returned "
                f"{type(payload).__name__}, expected an array of postings.",
                portal=self.slug,
            )

        clean = [record for record in payload if isinstance(record, dict)]
        self._cache[board.slug] = clean
        logger.info(
            "lever_board_loaded",
            portal=self.slug,
            board=board.slug,
            count=len(clean),
        )
        return clean

    # --- Parsing -----------------------------------------------------------

    def _to_raw_job(
        self, record: dict[str, Any], *, board: LeverBoard
    ) -> RawJob | None:
        """Map one Lever posting onto the shared RawJob contract.

        Returns None when the posting lacks the minimum needed to be usable -
        an id, a title and a link. A listing the user cannot open is not a
        result.
        """
        job_id = str(record.get("id") or "").strip()
        title = str(record.get("text") or "").strip()
        hosted_url = str(record.get("hostedUrl") or "").strip()

        if not job_id or not title or not hosted_url:
            logger.debug(
                "lever_record_skipped",
                portal=self.slug,
                board=board.slug,
                has_id=bool(job_id),
                has_title=bool(title),
                has_url=bool(hosted_url),
            )
            return None

        apply_url = str(record.get("applyUrl") or "").strip()

        return RawJob(
            external_id=job_id,
            source=self.slug,
            title=title,
            # Configuration, not the payload: Lever publishes no employer name.
            company=board.company,
            location=format_location(record),
            description=build_description(record),
            source_url=hosted_url,
            application_url=apply_url or None,
            posted_at=parse_created_at(record.get("createdAt")),
            employment_type=self._employment_type(record),
            # Lever is not a job board that links to an employer's application
            # - it *is* the employer's application system, and `applyUrl` is
            # the posting's own form. There is no one-click path through this
            # feed to be wrong about, so the declaration is a statement of what
            # the surface is rather than a guess about an individual listing.
            raw_payload=declare_application_type(
                dict(record), ApplicationType.LONG_APPLICATION
            ),
        )

    @staticmethod
    def _employment_type(record: dict[str, Any]) -> str | None:
        """The commitment the board published, when it published one.

        Lever's own field, left empty rather than assumed to be full time when
        a board does not set it.
        """
        categories = record.get("categories")
        if not isinstance(categories, dict):
            return None
        commitment = categories.get("commitment")
        if isinstance(commitment, str) and commitment.strip():
            return commitment.strip()
        return None


__all__ = [
    "API_BASE",
    "DEFAULT_BOARDS",
    "LeverBoard",
    "LeverPortal",
    "build_description",
    "format_location",
    "html_to_text",
    "parse_created_at",
    "title_matches",
    "title_tokens",
]
