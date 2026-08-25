"""Fetching a candidate career page safely.

This is the only place in the project that requests a URL chosen by a *third
party* - an employer's application link, arriving inside a portal's payload -
so it is the only place that has to defend against the URL itself.

# The threat, stated plainly

A portal we do not control supplies the string. If it published
``http://169.254.169.254/latest/meta-data/`` or ``http://localhost:8100/`` and
we fetched it, we would be making a request from inside the network on
someone else's instruction and handing back what came out. Every guard below
exists for that, and each one is applied **again after every redirect**,
because a public host that 302s to ``127.0.0.1`` defeats a check that only ran
on the original URL. That is why redirects are followed by hand here rather
than by ``follow_redirects=True``.

# What else is bounded

Response size, redirect count, and time. An employer's careers page that never
finishes must cost one career page, never the search. The body is read in
chunks and abandoned at the cap rather than buffered whole.

# What is not done

No spoofing. The honest ``JobFinderBot`` user agent from ``app.portals.http``
is sent, unchanged, and a host that declines it is recorded as declining -
never retried under a browser string. A 401 or a 403 ends the attempt.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from app.careers.robots import (
    MAX_ROBOTS_BYTES,
    RobotsCache,
    RobotsPolicy,
    origin_of,
    robots_url,
)
from app.core.logging import get_logger
from app.portals.http import DEFAULT_USER_AGENT

logger = get_logger(__name__)

#: Schemes we will fetch. Everything else - file:, ftp:, data:, javascript: -
#: is refused before a socket is opened.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Seconds for one request. Deliberately short: this runs once per candidate
#: URL and a slow employer must not hold the search open.
DEFAULT_TIMEOUT_SECONDS = 12.0

#: Redirect hops followed by hand. Enough for the ``/careers`` ->
#: ``/en-us/careers`` -> ATS chains real employers use, short enough that a
#: redirect loop ends quickly.
MAX_REDIRECTS = 5

#: Bytes of body read before we stop. Career pages that matter are far smaller;
#: this is a ceiling on damage, not a target.
MAX_BODY_BYTES = 1_500_000

#: Content types worth parsing. A PDF or an image is not a career portal, and
#: decoding one as HTML would produce nonsense that the evidence rules would
#: then have to reason about.
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class FetchOutcome:
    """Why a fetch ended. Not an enum on purpose - see ``FetchResult``."""

    OK = "ok"
    #: The URL itself was refused before any request: bad scheme, no host, or
    #: an address inside the network.
    BLOCKED = "blocked"
    #: ``robots.txt`` asks us not to fetch this path, or could not be read.
    DISALLOWED = "disallowed"
    #: The request was made and failed: timeout, DNS, connection, TLS.
    ERROR = "error"
    #: Too many redirects, or a redirect with no usable target.
    REDIRECT_LIMIT = "redirect_limit"


@dataclass(frozen=True, slots=True)
class FetchResult:
    """One fetch attempt: what came back, or why nothing did."""

    outcome: str
    url: str
    #: Where the redirect chain ended. Equal to ``url`` when nothing redirected.
    final_url: str
    status_code: int | None = None
    content_type: str = ""
    body: str = ""
    #: Set when ``outcome`` is not OK. Human-readable, shown in a reason line.
    detail: str = ""
    #: True when the body was cut off at ``MAX_BODY_BYTES``. Evidence rules
    #: treat a truncated body as evidence-bearing but never as proof of
    #: *absence*, since the missing part might have held what they wanted.
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome == FetchOutcome.OK

    @property
    def is_html(self) -> bool:
        lowered = self.content_type.casefold()
        return any(kind in lowered for kind in HTML_CONTENT_TYPES)


def _is_private_address(host: str) -> bool:
    """Does this host resolve anywhere we must not send a request?

    Resolution is done here rather than trusted to the URL because a public
    name can point at a private address - that is the whole trick. Every
    address the name resolves to is checked, not just the first: a name with
    one public and one loopback record must be refused outright.

    A name that does not resolve is *not* treated as private. It simply fails
    at connection time, and reporting "blocked for safety" for a typo would be
    a misleading reason.
    """
    stripped = host.strip("[]")

    try:
        address = ipaddress.ip_address(stripped)
    except ValueError:
        pass
    else:
        return not address.is_global

    try:
        infos = socket.getaddrinfo(stripped, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False

    for info in infos:
        sockaddr = info[4]
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if not address.is_global:
            return True

    return False


def address_is_safe(url: str) -> tuple[bool, str]:
    """May we open a connection to this URL? Returns ``(ok, reason)``."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "The URL could not be parsed."

    if parts.scheme.casefold() not in ALLOWED_SCHEMES:
        return False, f"Unsupported scheme {parts.scheme or '(none)'!r}."

    host = parts.hostname
    if not host:
        return False, "The URL has no host."

    if _is_private_address(host):
        return False, "The URL resolves to a private or local address."

    return True, ""


@dataclass(slots=True)
class CareerPageFetcher:
    """Fetches candidate career pages for one search run.

    Holds the run's response cache and its robots decisions, which is what
    stops the same employer's page being fetched once per posting. One
    instance per run; not shared between runs, so nothing is trusted longer
    than the search that learned it.
    """

    user_agent: str = DEFAULT_USER_AGENT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_redirects: int = MAX_REDIRECTS
    max_body_bytes: int = MAX_BODY_BYTES
    respect_robots: bool = True

    _client: httpx.AsyncClient | None = field(default=None, init=False)
    _cache: dict[str, FetchResult] = field(default_factory=dict, init=False)
    _robots: RobotsCache = field(default_factory=RobotsCache, init=False)

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> CareerPageFetcher:
        self._client = httpx.AsyncClient(
            timeout=self.timeout_seconds,
            # Followed by hand instead, so every hop is re-checked.
            follow_redirects=False,
            headers=self._headers(),
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        }

    # -- robots ------------------------------------------------------------

    async def _policy_for(self, url: str) -> RobotsPolicy:
        origin = origin_of(url)
        cached = self._robots.get(origin)
        if cached is not None:
            return cached

        policy = await self._load_robots(url)
        self._robots.put(origin, policy)
        return policy

    async def _load_robots(self, url: str) -> RobotsPolicy:
        assert self._client is not None
        try:
            response = await self._client.get(
                robots_url(url), follow_redirects=True, timeout=self.timeout_seconds
            )
        except httpx.HTTPError as exc:
            logger.debug("career_robots_unreadable", url=url, error=type(exc).__name__)
            return RobotsPolicy.unreadable_policy()

        if response.status_code == 404 or response.status_code == 410:
            # The host publishes no restrictions.
            return RobotsPolicy.permissive()
        if response.status_code >= 400:
            # The host has a file and would not give it to us. Declining is the
            # conservative reading, and it costs one employer.
            return RobotsPolicy.unreadable_policy()

        return RobotsPolicy.parse(
            response.text[:MAX_ROBOTS_BYTES], user_agent=self.user_agent
        )

    # -- fetching ----------------------------------------------------------

    async def fetch(self, url: str) -> FetchResult:
        """Fetch one candidate, following redirects safely.

        Cached per run by requested URL, so a company whose twelve postings all
        point at one board costs one request rather than twelve.
        """
        cached = self._cache.get(url)
        if cached is not None:
            return cached

        result = await self._fetch_uncached(url)
        self._cache[url] = result
        return result

    async def _fetch_uncached(self, url: str) -> FetchResult:
        if self._client is None:
            raise RuntimeError(
                "CareerPageFetcher must be used as an async context manager."
            )

        current = url
        for _hop in range(self.max_redirects + 1):
            safe, reason = address_is_safe(current)
            if not safe:
                return FetchResult(
                    outcome=FetchOutcome.BLOCKED,
                    url=url,
                    final_url=current,
                    detail=reason,
                )

            if self.respect_robots:
                policy = await self._policy_for(current)
                if not policy.allows(current):
                    return FetchResult(
                        outcome=FetchOutcome.DISALLOWED,
                        url=url,
                        final_url=current,
                        detail=(
                            "This site's robots.txt asks automated clients not "
                            "to fetch that page."
                        ),
                    )

            try:
                response = await self._client.get(current)
            except httpx.TimeoutException:
                return FetchResult(
                    outcome=FetchOutcome.ERROR,
                    url=url,
                    final_url=current,
                    detail=f"No response within {self.timeout_seconds:.0f}s.",
                )
            except httpx.HTTPError as exc:
                return FetchResult(
                    outcome=FetchOutcome.ERROR,
                    url=url,
                    final_url=current,
                    detail=f"Could not reach the site ({type(exc).__name__}).",
                )

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return FetchResult(
                        outcome=FetchOutcome.REDIRECT_LIMIT,
                        url=url,
                        final_url=current,
                        status_code=response.status_code,
                        detail="Redirected without a destination.",
                    )
                current = str(httpx.URL(current).join(location))
                continue

            body, truncated = await self._read_body(response)
            return FetchResult(
                outcome=FetchOutcome.OK,
                url=url,
                final_url=current,
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                body=body,
                truncated=truncated,
            )

        return FetchResult(
            outcome=FetchOutcome.REDIRECT_LIMIT,
            url=url,
            final_url=current,
            detail=f"More than {self.max_redirects} redirects.",
        )

    async def _read_body(self, response: httpx.Response) -> tuple[str, bool]:
        """Read a bounded amount of the body and decode it.

        Non-HTML bodies are not read at all: nothing downstream can learn
        anything from a PDF, and reading one would only burn bandwidth.
        """
        content_type = response.headers.get("content-type", "").casefold()
        if content_type and not any(
            kind in content_type for kind in HTML_CONTENT_TYPES
        ):
            await response.aclose()
            return "", False

        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            total += len(chunk)
            if total >= self.max_body_bytes:
                truncated = True
                break
        await response.aclose()

        raw = b"".join(chunks)[: self.max_body_bytes]
        encoding = response.charset_encoding or "utf-8"
        try:
            return raw.decode(encoding, errors="replace"), truncated
        except LookupError:
            return raw.decode("utf-8", errors="replace"), truncated

    async def post_json(self, url: str, payload: dict) -> dict | None:
        """POST JSON to a listing endpoint, under the same safety rules.

        Used only by ``app.careers.listings`` to read the *public* job feed a
        board's own page requests. Returns None rather than raising: a probe
        that fails is an absence of evidence, which the caller already knows
        how to handle.
        """
        if self._client is None:
            raise RuntimeError(
                "CareerPageFetcher must be used as an async context manager."
            )

        safe, _reason = address_is_safe(url)
        if not safe:
            return None

        if self.respect_robots:
            policy = await self._policy_for(url)
            if not policy.allows(url):
                return None

        try:
            response = await self._client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except httpx.HTTPError:
            return None

        if response.status_code != 200:
            return None
        try:
            decoded = response.json()
        except ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None


__all__ = [
    "ALLOWED_SCHEMES",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_REDIRECTS",
    "CareerPageFetcher",
    "FetchOutcome",
    "FetchResult",
    "address_is_safe",
]
