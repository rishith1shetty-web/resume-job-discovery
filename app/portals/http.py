"""Shared HTTP plumbing for portal adapters.

Timeouts, retries, backoff and rate limiting live here so every adapter gets
the same operational behaviour from its ``PortalConfig`` row rather than
hardcoding its own. What stays in the adapter is what is genuinely
portal-specific: URLs, parsing and field mapping.

Transport failures are translated into the ``app.portals.exceptions``
taxonomy, because that is what ``search_run_portals`` and ``portal_errors``
record — and what lets one portal fail without stopping the others.

Nothing here evades access controls. A 403 or a CAPTCHA challenge is reported
as ``PortalBlockedError`` and the portal is treated as unavailable; there is no
retry-with-a-different-fingerprint path, by design (CLAUDE.md section 15).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.core.logging import get_logger
from app.portals.base import Deadline
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalParseError,
    PortalRateLimitedError,
    PortalTimeoutError,
)

logger = get_logger(__name__)

#: Identifies this client honestly rather than impersonating a browser.
#: Portals that wish to block it can do so, which is the point.
DEFAULT_USER_AGENT = (
    "JobFinderBot/0.1 (+resume-based job discovery; contact via repository owner)"
)

#: The TLS trust store, built once for the whole process.
#:
#: Every request used to build its own. ``_send`` opens an ``httpx.AsyncClient``
#: per attempt, and constructing one parses the certifi CA bundle from disk -
#: measured at 451ms each, against 21ms for the rest of the context. That is
#: CPU rather than waiting, so it does not overlap while portals collect
#: concurrently: across a four-role benchmark it came to 962 seconds, 60% of
#: Workday's entire leg, and it was the single largest reason portals reached
#: their deadline with boards still unread.
#:
#: ``httpx.create_ssl_context()`` is asked for it rather than certifi directly,
#: for two reasons. It is the same bundle - the function's default path is
#: ``ssl.create_default_context(cafile=certifi.where())`` - so the trust store
#: is unchanged. And it keeps honouring ``SSL_CERT_FILE`` and ``SSL_CERT_DIR``,
#: which an operator may have set and which naming certifi directly would
#: silently ignore.
#:
#: Reusing one context is what ``ssl.SSLContext`` is designed for; a server
#: uses a single context for every connection it accepts. Nothing mutates this
#: one after construction, and it is never handed a client certificate.
#:
#: This does not share clients or connections. Each request still gets its own
#: client and its own connection, exactly as before - only the trust store is
#: no longer rebuilt.
SSL_CONTEXT = httpx.create_ssl_context()

#: Signals bot protection rather than a missing page. Never worked around.
_BLOCKED_STATUSES = frozenset({401, 403, 407, 451})
_RATE_LIMIT_STATUSES = frozenset({429})
#: Transient server-side failures worth one more attempt.
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504, 522, 524})

#: Longest ``Retry-After`` this client will actually wait out. A portal asking
#: for two minutes is asking for longer than a search can reasonably hold, so
#: past this the wait is declined and the portal is reported as rate-limited -
#: which is the truth, and leaves the other portals to finish.
MAX_RETRY_AFTER_SECONDS = 30.0

#: Shortest attempt worth starting. Below this there is not enough of the
#: deadline left for a connection to be established, let alone answered, so
#: starting one would only burn the margin the adapter needs to return under
#: its own power.
MIN_ATTEMPT_SECONDS = 0.5

#: The deadline governing whatever this task is currently doing, or ``None``
#: when nothing has set one.
#:
#: A context variable rather than a constructor argument or a parameter on
#: every call: the deadline belongs to *one search*, while a
#: ``PortalHttpClient`` belongs to an adapter and outlives any single query.
#: Threading it through by hand would mean changing the signature of every
#: fetch helper in every adapter to pass a value none of them make a decision
#: about - so it is carried the way other ambient, per-operation facts are.
#:
#: ``asyncio`` copies the current context into each new task, so a deadline set
#: here before ``asyncio.wait_for`` is visible inside the coroutine it wraps,
#: and inside anything that coroutine fans out with ``gather`` - which is what
#: makes this reach Workday's tenant fan-out without Workday knowing about it.
_ACTIVE_DEADLINE: ContextVar[Deadline | None] = ContextVar(
    "portal_active_deadline", default=None
)


def active_deadline() -> Deadline | None:
    """The deadline governing the current task, if one was set."""
    return _ACTIVE_DEADLINE.get()


@contextmanager
def use_deadline(deadline: Deadline | None) -> Iterator[None]:
    """Run a block with ``deadline`` governing every request it makes.

    Restores the previous value on exit, including when the body raises or is
    cancelled, so one portal's deadline can never leak into another's.
    """
    token = _ACTIVE_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _ACTIVE_DEADLINE.reset(token)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """How long the server explicitly asked us to wait, if it did.

    ``Retry-After`` comes in two forms: delta-seconds, and an HTTP date. Both
    are read, because a server that bothers to tell us when to come back has
    given us the only non-guessed backoff available.

    Returns None when the header is absent or unparseable, which is the signal
    to *not* retry at all. That distinction is the point of this function: a
    429 with no stated delay means we have no idea when the portal would
    welcome us, and inventing an interval is how a client ends up hammering a
    service that just asked it to stop.
    """
    raw = response.headers.get("retry-after")
    if not raw or not raw.strip():
        return None

    value = raw.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)

    return max(0.0, (when - datetime.now(UTC)).total_seconds())


@dataclass(slots=True)
class HttpPolicy:
    """Per-portal operational policy, mirroring the ``portal_configs`` row."""

    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_backoff_seconds: float = 2.0
    rate_limit_per_minute: int = 30
    user_agent: str = DEFAULT_USER_AGENT

    @classmethod
    def from_options(cls, options: dict | None) -> HttpPolicy:
        """Build from adapter options, ignoring unknown keys."""
        options = options or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in options.items() if key in known})

    @property
    def min_interval_seconds(self) -> float:
        """Smallest gap between requests that respects the rate limit."""
        if self.rate_limit_per_minute <= 0:
            return 0.0
        return 60.0 / self.rate_limit_per_minute


def _first_sse_datum(body: str) -> str:
    """The first ``data:`` payload in a server-sent-event frame.

    Returned unchanged when the body is not framed, so a server that answers a
    plain POST with plain JSON still works.
    """
    for line in body.splitlines():
        if line.startswith("data:"):
            return line[len("data:") :].strip()
    return body


@dataclass(slots=True)
class _RateLimiter:
    """Simple spacing limiter: wait until the next slot is due.

    Deliberately conservative — it paces requests rather than bursting to the
    limit, because the goal is to stay comfortably inside what a portal
    tolerates, not to extract the maximum allowed throughput.
    """

    min_interval: float
    _last_request_at: float = field(default=0.0)

    async def acquire(self, budget: float | None = None) -> bool:
        """Wait for this portal's next slot. False when it is out of reach.

        ``budget`` is how much of the caller's deadline is left. The spacing is
        never shortened to fit inside it: waiting less than ``min_interval``
        would be sending faster than the portal's configured rate limit allows,
        which is the one thing a deadline must not be allowed to buy. So when
        the slot falls beyond the budget this returns False and the caller
        stops, having sent nothing.
        """
        if self.min_interval <= 0:
            return True
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval - elapsed
        if wait > 0:
            if budget is not None and wait > budget:
                return False
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()
        return True


class PortalHttpClient:
    """Async HTTP client bound to one portal's policy."""

    def __init__(
        self,
        *,
        portal: str,
        policy: HttpPolicy | None = None,
        keep_cookies: bool = False,
    ) -> None:
        self.portal = portal
        self.policy = policy or HttpPolicy()
        self._limiter = _RateLimiter(self.policy.min_interval_seconds)
        # Off by default, so every existing portal keeps sending each request
        # from a clean client with no state carried between them.
        #
        # A portal that issues a token in one response and validates it on the
        # next - a double-submit CSRF cookie, say - cannot work that way: the
        # token and its cookie have to travel together. Opting in keeps one jar
        # for this client's lifetime, which is one search run.
        self._cookies: httpx.Cookies | None = httpx.Cookies() if keep_cookies else None

    @property
    def cookies(self) -> httpx.Cookies | None:
        """The session jar, when this client was built with ``keep_cookies``."""
        return self._cookies

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.policy.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def _send(
        self,
        send: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]],
        *,
        headers: dict[str, str] | None = None,
        absent_statuses: frozenset[int] = frozenset(),
    ) -> httpx.Response:
        """Run one request under this portal's timeout, retry and rate limit.

        The verb-specific part is the ``send`` callable, so GET and POST share
        one policy rather than drifting apart: a portal that offers a JSON-RPC
        endpoint must still honour the same ``portal_configs`` row as one that
        serves HTML.

        Raises a ``PortalError`` subclass on failure.

        When a deadline is active, no attempt is started that cannot finish
        inside it and no wait is served that would run past it. That is what
        lets an adapter come back with what it has: the alternative is the
        caller's ``asyncio.wait_for`` cancelling the coroutine mid-request,
        which destroys every listing collected so far because they exist only
        in that coroutine's local variables. The configured timeout is a
        ceiling here and stays one - the clamp can only ever shorten an
        attempt, never extend it past what the portal's policy allows.
        """
        attempts = max(1, self.policy.max_retries + 1)
        last_error: PortalError | None = None
        request_headers = {**self._headers(), **(headers or {})}
        deadline = active_deadline()

        def remaining() -> float | None:
            return None if deadline is None else deadline.remaining

        def out_of_time(need: float) -> bool:
            """True when ``need`` seconds are not there to be spent."""
            left = remaining()
            return left is not None and left < need

        for attempt in range(1, attempts + 1):
            # Checked before the slot as well as after it: a retry that cannot
            # finish is not worth the portal's rate-limit budget either.
            if out_of_time(MIN_ATTEMPT_SECONDS):
                last_error = last_error or PortalTimeoutError(
                    f"Request to {self.portal} abandoned: the search deadline "
                    "was reached before this attempt could be made.",
                    portal=self.portal,
                )
                logger.info(
                    "portal_request_deadline_reached",
                    portal=self.portal,
                    attempt=attempt,
                    stage="before_attempt",
                )
                break

            if not await self._limiter.acquire(budget=remaining()):
                last_error = last_error or PortalTimeoutError(
                    f"Request to {self.portal} abandoned: the next rate-limit "
                    "slot falls beyond the search deadline.",
                    portal=self.portal,
                )
                logger.info(
                    "portal_request_deadline_reached",
                    portal=self.portal,
                    attempt=attempt,
                    stage="rate_limit_slot",
                )
                break

            # The tighter of the portal's own timeout and what is left. Waiting
            # the full policy timeout on an attempt that outlives the deadline
            # is what produced the hard cancellation this guards against.
            left = remaining()
            attempt_timeout = self.policy.timeout_seconds
            if left is not None:
                attempt_timeout = min(attempt_timeout, left)
                if attempt_timeout < MIN_ATTEMPT_SECONDS:
                    break

            try:
                async with httpx.AsyncClient(
                    timeout=attempt_timeout,
                    follow_redirects=True,
                    headers=request_headers,
                    cookies=self._cookies,
                    verify=SSL_CONTEXT,
                ) as client:
                    if left is None:
                        response = await send(client)
                    else:
                        # httpx timeouts are per *operation*, not per request:
                        # a body that keeps arriving never trips the read
                        # timeout, so a multi-megabyte download can outlive any
                        # value passed above. Measured on Lever, whose board
                        # feeds run to 8MB, the clamp alone still let a request
                        # reach the query ceiling and be cancelled from
                        # outside. So the whole attempt is bounded by what is
                        # left - and only by that, never by the policy timeout,
                        # which stays the per-operation limit it has always
                        # been. A timeout here cancels the request; the adapter
                        # lives on and keeps what it collected.
                        response = await asyncio.wait_for(
                            send(client), timeout=left
                        )
                    if self._cookies is not None:
                        # Carry anything the portal set into the next request.
                        self._cookies.update(response.cookies)
            except TimeoutError:
                # `left` is None only on the unbounded path, where this can
                # only have come from the send callable itself rather than
                # from the bound above.
                bound = "the deadline" if left is None else f"{left:g}s"
                last_error = PortalTimeoutError(
                    f"Request to {self.portal} was stopped after {bound}: "
                    "the search deadline was reached.",
                    portal=self.portal,
                )
                logger.info(
                    "portal_request_deadline_reached",
                    portal=self.portal,
                    attempt=attempt,
                    stage="in_flight",
                )
                break
            except httpx.TimeoutException as exc:
                last_error = PortalTimeoutError(
                    f"Request to {self.portal} timed out after "
                    f"{attempt_timeout:g}s.",
                    portal=self.portal,
                )
                logger.warning(
                    "portal_request_timeout",
                    portal=self.portal,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
            except httpx.HTTPError as exc:
                last_error = PortalError(
                    f"Network error contacting {self.portal}: {type(exc).__name__}.",
                    portal=self.portal,
                )
                logger.warning(
                    "portal_request_network_error",
                    portal=self.portal,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
            else:
                status = response.status_code

                if status in absent_statuses:
                    # The caller declared this status a *fact about the
                    # resource* rather than a failure, so it is handed back
                    # instead of raised. Checked first, before the blanket
                    # `status >= 400` rule that would otherwise swallow the
                    # distinction: "this file does not exist" and "this file
                    # exists and I could not read it" are different answers,
                    # and a robots policy is the case where the difference
                    # decides whether anything may be fetched at all.
                    return response

                if status in _BLOCKED_STATUSES:
                    # Terminal by choice: the portal has said no, so stop
                    # asking. Retrying or disguising the client would be
                    # circumvention.
                    raise PortalBlockedError(
                        f"{self.portal} refused access (HTTP {status}). "
                        "Treating the portal as unavailable rather than "
                        "attempting to bypass the restriction.",
                        portal=self.portal,
                    )

                if status in _RATE_LIMIT_STATUSES:
                    # Retried only on the service's own terms. When it states a
                    # `Retry-After` we wait exactly that long, once, and try
                    # again; when it does not, we stop. There is deliberately no
                    # invented interval here - guessing at a delay after being
                    # asked to slow down is how a client turns a rate limit
                    # into a block.
                    delay = _retry_after_seconds(response)
                    if (
                        delay is not None
                        and delay <= MAX_RETRY_AFTER_SECONDS
                        and attempt < attempts
                        # Sleeping past the deadline would mean waking to be
                        # cancelled. The portal asked us to wait, and we stop
                        # instead of waiting - we never wait less than it asked.
                        and not out_of_time(delay + MIN_ATTEMPT_SECONDS)
                    ):
                        logger.info(
                            "portal_request_retry_after",
                            portal=self.portal,
                            attempt=attempt,
                            delay_seconds=round(delay, 1),
                        )
                        await asyncio.sleep(delay)
                        continue

                    raise PortalRateLimitedError(
                        f"{self.portal} rate-limited this client (HTTP {status}). "
                        "Backing off.",
                        portal=self.portal,
                    )

                if status in _RETRYABLE_STATUSES:
                    last_error = PortalError(
                        f"{self.portal} returned HTTP {status}.", portal=self.portal
                    )
                    logger.warning(
                        "portal_request_server_error",
                        portal=self.portal,
                        attempt=attempt,
                        status=status,
                    )
                elif status >= 400:
                    raise PortalError(
                        f"{self.portal} returned HTTP {status}.", portal=self.portal
                    )
                else:
                    return response

            if attempt < attempts:
                # Exponential backoff, so a struggling portal is not hammered.
                backoff = self.policy.retry_backoff_seconds * attempt
                if out_of_time(backoff + MIN_ATTEMPT_SECONDS):
                    # The backoff plus the attempt it protects will not fit.
                    # Stop now rather than sleeping into a cancellation - and
                    # do not shorten the backoff, which exists to spare a
                    # struggling portal, not to be traded for our own deadline.
                    logger.info(
                        "portal_request_deadline_reached",
                        portal=self.portal,
                        attempt=attempt,
                        stage="retry_backoff",
                    )
                    break
                await asyncio.sleep(backoff)

        raise last_error or PortalError(
            f"Request to {self.portal} failed.", portal=self.portal
        )

    async def get_text(self, url: str, *, params: dict | None = None) -> str:
        """GET a URL and return its body, honouring timeout, retry and rate limit.

        Raises a ``PortalError`` subclass on failure. Never returns an empty
        string to paper over a problem: the caller has to be able to tell a
        genuinely empty result from a broken one.
        """
        response = await self._send(lambda client: client.get(url, params=params))
        return response.text

    async def get_optional_text(
        self,
        url: str,
        *,
        absent_statuses: frozenset[int] = frozenset({404, 410}),
    ) -> str | None:
        """GET a URL whose absence is a meaningful answer, not a failure.

        Returns None when the server said the resource is not there, and the
        body otherwise. Every other failure still raises, so "absent" never
        gets confused with "unreachable".

        This exists for ``robots.txt``. ``app.careers.fetching`` already draws
        exactly this line - 404 and 410 mean the host publishes no restrictions
        and anything else means it has an opinion we could not read, which is
        declined - and a portal that must resolve robots per host at run time
        needs the same distinction. ``get_text`` cannot express it: it raises
        ``PortalError`` for 404 and for 500 alike, and recovering the status
        from the message text would be guesswork.
        """
        response = await self._send(
            lambda client: client.get(url), absent_statuses=absent_statuses
        )
        if response.status_code in absent_statuses:
            return None
        return response.text

    async def get_json(self, url: str, *, params: dict | None = None) -> dict:
        """GET a URL and return the decoded JSON object.

        For portals that publish a read-only JSON search endpoint. Decoding
        rules are shared with ``post_json`` so a malformed body is reported the
        same way whichever verb fetched it.
        """
        response = await self._send(lambda client: client.get(url, params=params))
        return self._decode_object(response)

    async def post_json(
        self,
        url: str,
        *,
        payload: dict,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """POST JSON and return the decoded object.

        For portals that publish a JSON or JSON-RPC endpoint rather than pages.
        A body that is not valid JSON is a ``PortalParseError``, not a crash:
        the run has to be able to record it against that one portal.

        Servers speaking the streamable-HTTP flavour of JSON-RPC may answer a
        plain POST with a one-event ``text/event-stream`` frame, so that shape
        is unwrapped here rather than in every adapter.
        """
        response = await self._send(
            lambda client: client.post(url, json=payload, headers=headers),
            headers=headers,
        )
        return self._decode_object(response)

    def _decode_object(self, response: httpx.Response) -> dict:
        """Decode a response body that is expected to be a JSON object.

        A body that is not valid JSON, or that is valid JSON but not an object,
        is a ``PortalParseError`` rather than a crash: the run has to be able to
        record it against this one portal and carry on with the others.
        """
        body = response.text
        if "text/event-stream" in response.headers.get("content-type", ""):
            body = _first_sse_datum(body)

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PortalParseError(
                f"{self.portal} returned a body that was not valid JSON: {exc.msg}.",
                portal=self.portal,
            ) from exc

        if not isinstance(decoded, dict):
            raise PortalParseError(
                f"{self.portal} returned {type(decoded).__name__}, expected an object.",
                portal=self.portal,
            )
        return decoded

    async def head_ok(self, url: str) -> bool:
        """Cheap reachability probe used by health checks."""
        await self._limiter.acquire()
        try:
            async with httpx.AsyncClient(
                timeout=self.policy.timeout_seconds,
                follow_redirects=True,
                headers=self._headers(),
                verify=SSL_CONTEXT,
            ) as client:
                response = await client.head(url)
            return response.status_code < 400
        except httpx.HTTPError:
            return False
