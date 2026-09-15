"""Ashby adapter (https://jobs.ashbyhq.com/).

Access surface, and why *this* one
----------------------------------
Ashby publishes each employer's board twice over, and this adapter deliberately
reads the slower of the two.

``jobs.ashbyhq.com/robots.txt`` is explicit::

    User-Agent: *
    Disallow: /meeting/
    Disallow: /b/
    Disallow: /api/

So the board page ``/<slug>`` and the job page ``/<slug>/<id>`` are permitted,
and the board's own internal API under ``/api/`` is not. Nothing here touches
it.

There is also ``api.ashbyhq.com/posting-api/job-board/<slug>``, which returns
an entire board - descriptions, dates, structured countries - in **one**
request. It is not used, and the reason is a rule this project already holds
itself to rather than a judgement about Ashby. That host answers ``401`` for
``robots.txt``, so its rules cannot be read at all, and
``app.careers.fetching`` treats exactly that case as a refusal: only ``404``
and ``410`` are read as "no restrictions published", and every other error is
``unreadable_policy()`` - decline. RFC 9309 would permit the fetch; this
project is stricter, every other portal here reads a robots file it could
actually retrieve, and a new portal is a bad reason to open the first
exception. The cost is a handful of extra requests, paid deliberately.

Ashby is an ATS, not a job board
--------------------------------
The same structural fact that shapes ``greenhouse.py``, ``lever.py`` and
``dayforce.py``: **Ashby publishes no cross-company search.**
``jobs.ashbyhq.com/`` is a 404, ``/search`` returns the application shell, the
posting API's board-list route is ``401``, and neither the marketing sitemap
nor the customers page carries a board URL. Each employer's board is addressed
by slug and there is no index to enumerate.

So collection works the other way round from a board: the configured boards are
fetched, and the search queries filter them by title. The boards are
**configuration, not code** - ``portal_configs.options["boards"]`` overrides the
default list, so adding an employer needs no deployment.

Two requests, and why the second one is cheap
---------------------------------------------
The board page carries every posting's title, id, location name, workplace type
and commitment - but **no publication date and no description**. Both of those
live on the job page. That would be ruinous if every job needed fetching, so
none of them does: the titles are filtered against the same-role variations
first, and only the handful that match are opened. A board of 95 postings
typically costs one request plus nought to three.

Nothing about a board page may be trusted on its status code. A slug that does
not exist answers **200** with the application shell - a soft 404 - so a board
is accepted only when the page actually carries an organisation name and a
posting collection. ``_read_board`` is where that is enforced.

What it gives in exchange
-------------------------
* **The employer's own name**, published on the page. Unlike Lever and
  Dayforce, no board has to be told who owns it.
* **The full posting text**, typically six thousand characters and already
  sectioned into responsibilities, qualifications and benefits.
* **A structured country** on the job page's ``schema.org`` data, so the United
  States rule is applied to a published fact rather than to a parsed string.
* **A stable UUID identity**, employer-direct apply URLs, and a real
  publication date.

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
)
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
)
from app.portals.http import HttpPolicy, PortalHttpClient

logger = get_logger(__name__)

#: The board host. Every URL this adapter builds starts here, and none of them
#: enters ``/api/``, ``/b/`` or ``/meeting/``.
BOARD_BASE = "https://jobs.ashbyhq.com"

#: The host holding the one-request posting API. Named only so the tests can
#: assert that nothing ever requests it - see the module docstring for why.
FORBIDDEN_API_HOST = "api.ashbyhq.com"

#: Employer boards read unless an operator configures otherwise. Override with
#: ``portal_configs.options["boards"]``, a list of slugs.
#:
#: Every slug here was fetched before being added: each returned a board page
#: carrying an organisation name and a posting collection, and each was
#: measured for United States postings inside the 30-day window. Slugs are
#: hypotheses until proved - 108 of 169 plausible company names returned no
#: board at all - so nothing may be added here by inference from a name.
#:
#: Employer names are deliberately absent. Ashby publishes the organisation on
#: the board page itself, so unlike ``lever.py`` and ``dayforce.py`` this
#: adapter never has to be told who owns a board, and a configured board cannot
#: carry a stale or wrong company name.
#:
#: Ordered roughly by measured fresh-US volume, because the walk stops once the
#: request's limit is met and the richest boards should be reached first.
DEFAULT_BOARDS: tuple[str, ...] = (
    "openai",
    "snowflake",
    "plaid",
    "perplexity",
    "ramp",
    "cohere",
    "elevenlabs",
    "whoop",
    "vanta",
    "handshake",
    "cerebras",
    "replit",
    "benchling",
    "notion",
    "temporal",
    "poshmark",
    "clickup",
    "sentry",
    "strava",
    "watershed",
    "prefect",
    "render",
    "airbyte",
    "hex",
    "circle",
    "sardine",
    "astronomer",
    "alchemy",
)

_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_WORD_RE = re.compile(r"[a-z0-9]+")
_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_APP_DATA_MARKER = "window.__appData"

#: Single letters match nearly every title and carry no signal: "FP&A Analyst"
#: must require "fp" and "analyst", not "a".
_MIN_TOKEN_LENGTH = 2

#: How Ashby spells the United States. Both forms occur in live data - measured
#: 1,162 "United States" against 237 "USA" on one sample - so a naive
#: equality test against either one silently discards the other's postings.
US_COUNTRY_NAMES = frozenset(
    {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
)

#: Ashby's own vocabulary for how a role is worked. Surfaced into the
#: description as a labelled line so the shared ``detect_work_mode`` reads it -
#: ``RawJob`` has no work-mode field, and adding one would change the adapter
#: contract for every other portal.
_WORKPLACE_TYPES = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "onsite": "Onsite",
}

#: Ashby publishes commitments in camel case; these are the ones seen live.
#: An unrecognised value is passed through as published rather than dropped.
_EMPLOYMENT_TYPES = {
    "fulltime": "Full-time",
    "parttime": "Part-time",
    "intern": "Intern",
    "contract": "Contract",
    "temporary": "Temporary",
}


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

    Deliberately blunt: this is the search Ashby does not provide, not a
    scoring step. The role gate downstream is what actually decides relevance,
    and it is the authority (CLAUDE.md section 7).

    Character-for-character the rule ``greenhouse.py``, ``lever.py``,
    ``dayforce.py`` and ``google_careers.py`` each state for themselves, and
    stated here for the same reason they do: no adapter in this package imports
    another, so a portal can be changed or retired without disturbing the rest.
    ``tests/test_ashby_adapter.py`` pins this to Greenhouse's answer over a
    shared table of cases, so the copies cannot drift apart unnoticed.

    It also earns its keep twice here. Every title it rejects is a job page
    this adapter never requests.
    """
    wanted = title_tokens(query)
    return bool(wanted) and wanted <= title_tokens(title)


def html_to_text(value: Any) -> str | None:
    """Flatten one of Ashby's HTML fragments into readable plain text.

    Ashby serves descriptions as markup with real headings - ``<h1>About the
    role</h1>``, ``<h2>Requirements</h2>`` - so block boundaries become
    newlines and list items gain a bullet. Those headings are what the matching
    layer reads to tell a stated requirement from prose, which is why this
    flattens rather than strips.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    markup = html.unescape(value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", markup)
    text = re.sub(r"(?i)</(p|div|li|ul|ol|h[1-6]|tr|section)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_RUN_RE.sub("\n\n", text).strip()

    return text or None


def parse_app_data(page: str) -> dict[str, Any] | None:
    """The ``window.__appData`` island a rendered Ashby page carries.

    Read by matching brackets rather than by regular expression, because the
    island is a single JSON object several tens of kilobytes long containing
    both braces and escaped quotes inside its own strings; a non-greedy pattern
    stops at the first ``}`` in a description and a greedy one swallows the
    rest of the document.

    Returns None when the page has no island or it will not parse. That is not
    an error here - the caller decides what a page without data means, and for
    a board page it means the slug does not exist.
    """
    if not isinstance(page, str):
        return None

    marker = page.find(_APP_DATA_MARKER)
    if marker < 0:
        return None
    start = page.find("{", marker)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(page)):
        char = page[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(page[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None

    return None


def parse_job_posting_ld(page: str) -> dict[str, Any] | None:
    """The ``schema.org`` JobPosting a job page publishes, if it has one.

    Ashby emits this block in two shapes - a bare object on most pages and an
    array of objects on others, both seen live on the same board - so every
    block is scanned and the first ``JobPosting`` wins. A page whose markup is
    not parseable simply has no posting, and the caller drops that listing
    rather than guessing at its date.
    """
    if not isinstance(page, str):
        return None

    for block in _LD_RE.findall(page):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item

    return None


def parse_date_posted(value: Any) -> datetime | None:
    """Ashby's ``datePosted``, as an aware UTC datetime.

    This is the only date the adapter will use. It is the original publication
    date: verified against the posting API's ``publishedAt`` on eight postings
    across four employers, ages spanning 0 to 1,285 days, every one agreeing
    exactly - a posting first published in March 2023 still declares
    ``2023-03-09``. Ashby does not restamp it, and it publishes no update or
    refresh timestamp that could be mistaken for it.

    A posting whose date is missing or unparseable keeps ``posted_at`` empty
    rather than being given a substitute. The freshness gate then excludes it,
    which is what CLAUDE.md section 6 requires of a date that cannot be
    verified - and is why the fetch time is never used here.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _countries(job_location: Any) -> list[str]:
    """Every country named by a ``jobLocation``, whichever shape it arrived in.

    ``jobLocation`` is an object on most postings and an array on
    multi-location ones; both occur on the same board.
    """
    entries = job_location if isinstance(job_location, list) else [job_location]
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if not isinstance(address, dict):
            continue
        country = address.get("addressCountry")
        if isinstance(country, str) and country.strip():
            names.append(country.strip())
    return names


def is_us_posting(posting: dict[str, Any]) -> bool:
    """Whether Ashby itself places this role in the United States.

    Reads the structured country the job page publishes, accepting both
    spellings Ashby uses. A multi-location role counts when *any* of its
    locations is in the United States, because a role hiring in New York and
    Dublin is genuinely open to a US candidate.

    This decides which published location to report, not whether the listing is
    eligible. The verdict itself is still made downstream by
    ``app.processing.us_locations`` against the location string, and nothing
    here relabels a role the employer placed abroad.
    """
    for country in _countries(posting.get("jobLocation")):
        if country.casefold() in US_COUNTRY_NAMES:
            return True

    requirement = posting.get("applicantLocationRequirements")
    entries = requirement if isinstance(requirement, list) else [requirement]
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str) and name.casefold() in US_COUNTRY_NAMES:
                return True

    return False


def format_location(
    ld_posting: dict[str, Any], board_posting: dict[str, Any]
) -> str | None:
    """The role's location as the employer published it.

    Prefers a United States entry when a US-coded role lists several - the same
    preference ``dayforce.py``, ``google_careers.py`` and ``lever.py`` apply -
    because this product returns US vacancies only and the US location is the
    one that matters.

    The country gates that preference. A role Ashby places in Ireland keeps its
    published location even if a secondary office reads like a US city, because
    the alternative is inferring a country the employer did not claim.

    The verdict itself is still made downstream by
    ``app.processing.us_locations`` against this string.
    """
    if is_us_posting(ld_posting):
        entries = ld_posting.get("jobLocation")
        entries = entries if isinstance(entries, list) else [entries]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            address = entry.get("address")
            if not isinstance(address, dict):
                continue
            country = address.get("addressCountry")
            if not isinstance(country, str):
                continue
            if country.casefold() not in US_COUNTRY_NAMES:
                continue
            parts = [
                str(address.get(field) or "").strip()
                for field in ("addressLocality", "addressRegion")
            ]
            formatted = ", ".join(part for part in parts if part)
            if formatted:
                return formatted

        # Structured parts were empty - common on fully remote roles, where
        # Ashby states the country but no city. The board's own label is then
        # the most accurate thing published ("Remote U.S.").
        for candidate in (
            board_posting.get("locationName"),
            ld_posting.get("jobLocationType") == "TELECOMMUTE" and "Remote, United States",
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

    published = board_posting.get("locationName")
    if isinstance(published, str) and published.strip():
        return published.strip()
    return None


def build_description(
    ld_posting: dict[str, Any], board_posting: dict[str, Any]
) -> str | None:
    """The employer's full posting text.

    ``schema.org``'s ``description`` is preferred because it is the text Ashby
    publishes for consumption, and it is complete: measured across 1,571 live
    postings it runs a median of 6,205 characters, never empty and never
    truncated. The page's own ``descriptionHtml`` says the same thing and is
    used only when the structured block is missing.

    Both are markup, so both are flattened by ``html_to_text``, which keeps the
    headings - Responsibilities, Minimum Qualifications, Benefits - that the
    matching layer reads.
    """
    parts: list[str] = []

    body = html_to_text(ld_posting.get("description"))
    if not body:
        body = html_to_text(board_posting.get("descriptionHtml"))
    if body:
        parts.append(body)

    if not parts:
        return None

    # How the role is worked, when Ashby published it. `RawJob` carries no
    # field for this, so it goes into the text the shared work-mode detector
    # already reads. Only Ashby's own vocabulary is emitted - an unrecognised
    # value is dropped rather than guessed at.
    workplace = str(board_posting.get("workplaceType") or "").strip().casefold()
    label = _WORKPLACE_TYPES.get(workplace)
    if label:
        parts.insert(0, f"Workplace type: {label}")

    text = "\n\n".join(parts).strip()
    return text or None


def employment_type(board_posting: dict[str, Any]) -> str | None:
    """The commitment the board published, when it published one."""
    raw = board_posting.get("employmentType")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return _EMPLOYMENT_TYPES.get(raw.strip().casefold(), raw.strip())


class AshbyPortal(BasePortal):
    """Collects listings from Ashby's public, robots-permitted board pages."""

    slug: ClassVar[str] = "ashby"
    display_name: ClassVar[str] = "Ashby"

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self._http = PortalHttpClient(
            portal=self.slug, policy=HttpPolicy.from_options(self.options)
        )
        self._boards = self._configured_boards(self.options)
        #: Board pages already fetched during this run, as
        #: ``slug -> (organisation, postings)``. Every query filters the same
        #: boards, so without this each same-role variation would re-download
        #: every board.
        self._boards_cache: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        #: Job pages already fetched during this run. A posting that answers
        #: two title variations must not be requested twice.
        self._jobs_cache: dict[str, RawJob | None] = {}

    @staticmethod
    def _configured_boards(options: dict[str, Any]) -> tuple[str, ...]:
        """Board slugs from configuration, falling back to the defaults.

        Slugs only. The employer's name is published on the board page, so
        unlike the other per-employer adapters here there is nothing else to
        configure and nothing that can go stale.
        """
        configured = options.get("boards")
        if isinstance(configured, str):
            configured = [configured]
        if isinstance(configured, list | tuple):
            slugs: list[str] = []
            for entry in configured:
                # Accept the mapping form too, so an operator who copies the
                # Lever or Dayforce shape is not silently ignored.
                if isinstance(entry, dict):
                    entry = entry.get("slug")
                if isinstance(entry, str) and entry.strip():
                    slugs.append(entry.strip())
            if slugs:
                return tuple(slugs)
        return DEFAULT_BOARDS

    @property
    def boards(self) -> tuple[str, ...]:
        return self._boards

    @property
    def capabilities(self) -> PortalCapabilities:
        return PortalCapabilities(
            # No date filter anywhere on the board; freshness is applied by the
            # caller against posted_at, which datePosted makes exact.
            supports_date_filter=False,
            # `search` already returns the full posting text: the job page it
            # reads for the date carries the description as well, so there is
            # nothing left for a second pass to fetch.
            supports_description_fetch=False,
            # The board page carries every posting at once, and the feed's
            # paging parameters are ignored, so there are no pages to walk.
            supports_pagination=False,
            requires_auth=False,
            provides_application_url=True,
        )

    # --- URLs --------------------------------------------------------------

    def board_url(self, slug: str) -> str:
        return f"{BOARD_BASE}/{slug}"

    def job_url(self, slug: str, job_id: str) -> str:
        return f"{BOARD_BASE}/{slug}/{job_id}"

    def apply_url(self, slug: str, job_id: str) -> str:
        """Ashby's own application path for a posting.

        Built rather than read because the board page does not publish it, and
        it is the site's own scheme rather than an invention: the posting API
        returns exactly ``<jobUrl>/application`` for every record, and the page
        links there.
        """
        return f"{self.job_url(slug, job_id)}/application"

    # --- Collection --------------------------------------------------------

    async def search(self, request: PortalSearchRequest) -> PortalSearchResult:
        """Collect listings matching the request's same-role title variations.

        Boards are read independently: one board being unavailable costs that
        employer's listings, not the whole pass. Job pages are opened only for
        titles that already match, so a board of a hundred postings normally
        costs one request.
        """
        seen: set[str] = set()
        jobs: list[RawJob] = []
        failures: list[str] = []

        for slug in self._boards:
            if len(jobs) >= request.limit:
                break

            try:
                organisation, postings = await self._read_board(slug)
            except (PortalBlockedError, PortalRateLimitedError):
                # A refusal is respected immediately and for the whole portal:
                # moving on to the next board would be ignoring what the site
                # just said.
                raise
            except PortalError as exc:
                logger.info(
                    "ashby_board_unavailable",
                    portal=self.slug,
                    board=slug,
                    error=exc.message,
                )
                failures.append(slug)
                continue

            for posting in postings:
                if len(jobs) >= request.limit:
                    break

                title = str(posting.get("title") or "").strip()
                if not title:
                    continue
                if not any(title_matches(query, title) for query in request.queries):
                    continue

                job_id = str(posting.get("id") or "").strip()
                if not job_id or job_id in seen:
                    continue

                raw = await self._read_job(
                    slug, organisation=organisation, posting=posting
                )
                if raw is None:
                    continue

                seen.add(job_id)
                jobs.append(raw)

        if failures and len(failures) == len(self._boards):
            # Every board failed: that is the portal being down, and the run
            # must record it rather than reporting "no matching jobs".
            raise PortalError(
                f"No Ashby board could be read ({len(failures)} attempted).",
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
        """Reachability of the board host, via the first configured board."""
        slug = self._boards[0] if self._boards else DEFAULT_BOARDS[0]
        try:
            await self._read_board(slug)
        except Exception as exc:  # noqa: BLE001 - a health check reports, never raises
            return PortalHealth(
                is_available=False,
                checked_at=datetime.now(UTC),
                detail=f"Board page did not respond: {type(exc).__name__}.",
            )
        return PortalHealth(is_available=True, checked_at=datetime.now(UTC))

    # --- Fetching ----------------------------------------------------------

    async def _read_board(self, slug: str) -> tuple[str, list[dict[str, Any]]]:
        """One employer's board: who they are, and what they have posted.

        Cached for the life of this adapter, which is one search run.

        A slug that does not exist answers **200** with the application shell,
        so the status code proves nothing. A board counts as real only when the
        page carries an organisation name *and* a posting collection; anything
        else is treated as an unavailable board and isolated to that employer.
        An employer with a genuinely empty board still publishes its name and
        an empty list, which is how "no openings today" stays distinguishable
        from "no such board".
        """
        if slug in self._boards_cache:
            return self._boards_cache[slug]

        page = await self._http.get_text(self.board_url(slug))
        data = parse_app_data(page)
        if data is None:
            raise PortalParseError(
                f"Ashby board {slug} served no readable page data.",
                portal=self.slug,
            )

        organisation = data.get("organization")
        name = ""
        if isinstance(organisation, dict):
            name = str(organisation.get("name") or "").strip()

        board = data.get("jobBoard")
        postings = board.get("jobPostings") if isinstance(board, dict) else None

        if not name or not isinstance(postings, list):
            # The soft 404. Reported as an unavailable board rather than an
            # empty one, because returning "no jobs" for a slug that does not
            # exist would hide a broken configuration for ever.
            raise PortalError(
                f"Ashby board {slug} does not exist "
                f"(no organisation or posting list on the page).",
                portal=self.slug,
            )

        clean = [item for item in postings if isinstance(item, dict)]
        self._boards_cache[slug] = (name, clean)
        logger.info(
            "ashby_board_loaded",
            portal=self.slug,
            board=slug,
            organisation=name,
            count=len(clean),
        )
        return name, clean

    async def _read_job(
        self, slug: str, *, organisation: str, posting: dict[str, Any]
    ) -> RawJob | None:
        """One posting's own page, which is where the date and text live.

        Returns None when the page cannot be read or carries no publication
        date. A listing whose date is unknown could not pass the freshness gate
        anyway, and dropping it here costs nothing and keeps a dateless record
        out of the pipeline.
        """
        job_id = str(posting.get("id") or "").strip()
        if not job_id:
            return None
        if job_id in self._jobs_cache:
            return self._jobs_cache[job_id]

        url = self.job_url(slug, job_id)
        try:
            page = await self._http.get_text(url)
        except (PortalBlockedError, PortalRateLimitedError):
            raise
        except PortalError as exc:
            logger.info(
                "ashby_job_unavailable",
                portal=self.slug,
                board=slug,
                job=job_id,
                error=exc.message,
            )
            self._jobs_cache[job_id] = None
            return None

        raw = self._to_raw_job(
            page, slug=slug, organisation=organisation, posting=posting
        )
        self._jobs_cache[job_id] = raw
        return raw

    # --- Parsing -----------------------------------------------------------

    def _to_raw_job(
        self,
        page: str,
        *,
        slug: str,
        organisation: str,
        posting: dict[str, Any],
    ) -> RawJob | None:
        """Map one Ashby posting onto the shared RawJob contract."""
        job_id = str(posting.get("id") or "").strip()
        title = str(posting.get("title") or "").strip()
        if not job_id or not title:
            return None

        ld = parse_job_posting_ld(page)
        page_data = parse_app_data(page) or {}
        page_posting = page_data.get("posting")
        page_posting = page_posting if isinstance(page_posting, dict) else {}

        # The board listing and the job page describe the same posting; the
        # page is the more complete of the two, so it wins where they overlap.
        merged: dict[str, Any] = {**posting, **page_posting}

        if ld is None:
            logger.info(
                "ashby_job_without_structured_data",
                portal=self.slug,
                board=slug,
                job=job_id,
            )
            return None

        posted_at = parse_date_posted(ld.get("datePosted"))
        if posted_at is None:
            logger.info(
                "ashby_job_without_publication_date",
                portal=self.slug,
                board=slug,
                job=job_id,
            )
            return None

        description = build_description(ld, merged)
        if description is None:
            return None

        # The page's own organisation, falling back to the board's. Ashby
        # publishes it in both places and they agree; neither is configuration.
        hiring = ld.get("hiringOrganization")
        company = ""
        if isinstance(hiring, dict):
            company = str(hiring.get("name") or "").strip()
        page_org = page_data.get("organization")
        if not company and isinstance(page_org, dict):
            company = str(page_org.get("name") or "").strip()

        return RawJob(
            external_id=job_id,
            source=self.slug,
            title=title,
            company=company or organisation or None,
            location=format_location(ld, merged),
            description=description,
            source_url=self.job_url(slug, job_id),
            application_url=self.apply_url(slug, job_id),
            posted_at=posted_at,
            employment_type=employment_type(merged),
            # Ashby is not a job board that links to an employer's application
            # - it *is* the employer's application system, and the apply path
            # is the posting's own form. There is no one-click route through
            # these pages to be wrong about, so the declaration is a statement
            # of what the surface is rather than a guess about one listing.
            raw_payload=declare_application_type(
                {"board": slug, "posting": merged, "jobPosting": ld},
                ApplicationType.LONG_APPLICATION,
            ),
        )


__all__ = [
    "BOARD_BASE",
    "DEFAULT_BOARDS",
    "FORBIDDEN_API_HOST",
    "US_COUNTRY_NAMES",
    "AshbyPortal",
    "build_description",
    "employment_type",
    "format_location",
    "html_to_text",
    "is_us_posting",
    "parse_app_data",
    "parse_date_posted",
    "parse_job_posting_ld",
    "title_matches",
    "title_tokens",
]
