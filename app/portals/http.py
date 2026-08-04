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
import time
from dataclasses import dataclass, field

import httpx

from app.core.logging import get_logger
from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalRateLimitedError,
    PortalTimeoutError,
)

logger = get_logger(__name__)

#: Identifies this client honestly rather than impersonating a browser.
#: Portals that wish to block it can do so, which is the point.
DEFAULT_USER_AGENT = (
    "JobFinderBot/0.1 (+resume-based job discovery; contact via repository owner)"
)

#: Signals bot protection rather than a missing page. Never worked around.
_BLOCKED_STATUSES = frozenset({401, 403, 407, 451})
_RATE_LIMIT_STATUSES = frozenset({429})
#: Transient server-side failures worth one more attempt.
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504, 522, 524})


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


@dataclass(slots=True)
class _RateLimiter:
    """Simple spacing limiter: wait until the next slot is due.

    Deliberately conservative — it paces requests rather than bursting to the
    limit, because the goal is to stay comfortably inside what a portal
    tolerates, not to extract the maximum allowed throughput.
    """

    min_interval: float
    _last_request_at: float = field(default=0.0)

    async def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()


class PortalHttpClient:
    """Async HTTP client bound to one portal's policy."""

    def __init__(self, *, portal: str, policy: HttpPolicy | None = None) -> None:
        self.portal = portal
        self.policy = policy or HttpPolicy()
        self._limiter = _RateLimiter(self.policy.min_interval_seconds)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.policy.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def get_text(self, url: str, *, params: dict | None = None) -> str:
        """GET a URL and return its body, honouring timeout, retry and rate limit.

        Raises a ``PortalError`` subclass on failure. Never returns an empty
        string to paper over a problem: the caller has to be able to tell a
        genuinely empty result from a broken one.
        """
        attempts = max(1, self.policy.max_retries + 1)
        last_error: PortalError | None = None

        for attempt in range(1, attempts + 1):
            await self._limiter.acquire()
            try:
                async with httpx.AsyncClient(
                    timeout=self.policy.timeout_seconds,
                    follow_redirects=True,
                    headers=self._headers(),
                ) as client:
                    response = await client.get(url, params=params)
            except httpx.TimeoutException as exc:
                last_error = PortalTimeoutError(
                    f"Request to {self.portal} timed out after "
                    f"{self.policy.timeout_seconds}s.",
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
                    return response.text

            if attempt < attempts:
                # Exponential backoff, so a struggling portal is not hammered.
                await asyncio.sleep(self.policy.retry_backoff_seconds * attempt)

        raise last_error or PortalError(
            f"Request to {self.portal} failed.", portal=self.portal
        )

    async def head_ok(self, url: str) -> bool:
        """Cheap reachability probe used by health checks."""
        await self._limiter.acquire()
        try:
            async with httpx.AsyncClient(
                timeout=self.policy.timeout_seconds,
                follow_redirects=True,
                headers=self._headers(),
            ) as client:
                response = await client.head(url)
            return response.status_code < 400
        except httpx.HTTPError:
            return False
