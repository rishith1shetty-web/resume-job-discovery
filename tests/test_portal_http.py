"""Shared portal HTTP policy: timeouts, retries, rate limiting, error mapping.

Every adapter inherits this behaviour from its ``portal_configs`` row, so it is
tested once here rather than per portal. All transport is stubbed; nothing in
this file makes a real request.
"""

from __future__ import annotations

import httpx
import pytest

from app.portals.exceptions import (
    PortalBlockedError,
    PortalError,
    PortalRateLimitedError,
    PortalTimeoutError,
)
from app.portals.http import DEFAULT_USER_AGENT, HttpPolicy, PortalHttpClient


def _client(**overrides) -> PortalHttpClient:
    defaults = {
        "timeout_seconds": 1.0,
        "max_retries": 2,
        # Keep tests fast. That retries happen at all is what is asserted.
        "retry_backoff_seconds": 0.0,
        # Pacing off unless a test opts in.
        "rate_limit_per_minute": 0,
    }
    return PortalHttpClient(
        portal="testportal", policy=HttpPolicy(**{**defaults, **overrides})
    )


@pytest.fixture
def patch_get(monkeypatch: pytest.MonkeyPatch):
    def _apply(handler):
        calls: list[str] = []

        async def _get(self, url, params=None):  # noqa: ANN001
            calls.append(url)
            return handler(len(calls), url, params)

        monkeypatch.setattr(httpx.AsyncClient, "get", _get)
        return calls

    return _apply


def _response(status: int, text: str = "ok") -> httpx.Response:
    return httpx.Response(status_code=status, text=text, request=httpx.Request("GET", "https://example.invalid"))


# --- Policy -----------------------------------------------------------------


def test_policy_is_built_from_the_portal_config_row() -> None:
    policy = HttpPolicy.from_options(
        {
            "timeout_seconds": 12.5,
            "max_retries": 4,
            "rate_limit_per_minute": 15,
            # Adapter-specific keys must not break policy construction.
            "access_authorized": True,
            "board_token": "abc",
        }
    )

    assert policy.timeout_seconds == 12.5
    assert policy.max_retries == 4
    assert policy.rate_limit_per_minute == 15


def test_the_client_identifies_itself_honestly() -> None:
    # Not a browser impersonation string: a portal that wants to block this
    # client must be able to.
    assert "JobFinderBot" in DEFAULT_USER_AGENT
    assert "Mozilla" not in DEFAULT_USER_AGENT


def test_rate_limit_becomes_a_minimum_interval() -> None:
    assert HttpPolicy(rate_limit_per_minute=30).min_interval_seconds == 2.0
    assert HttpPolicy(rate_limit_per_minute=0).min_interval_seconds == 0.0


async def test_requests_are_paced_by_the_rate_limit(patch_get) -> None:
    import time

    patch_get(lambda attempt, url, params: _response(200))
    client = PortalHttpClient(
        portal="testportal",
        policy=HttpPolicy(rate_limit_per_minute=120, retry_backoff_seconds=0.0),
    )

    started = time.monotonic()
    await client.get_text("https://example.invalid/a")
    await client.get_text("https://example.invalid/b")
    elapsed = time.monotonic() - started

    # 120/minute means half a second apart.
    assert elapsed >= 0.4


# --- Success ----------------------------------------------------------------


async def test_a_successful_response_returns_its_body(patch_get) -> None:
    patch_get(lambda attempt, url, params: _response(200, "<html>hi</html>"))

    assert await _client().get_text("https://example.invalid") == "<html>hi</html>"


# --- Retries ----------------------------------------------------------------


async def test_a_server_error_is_retried_then_succeeds(patch_get) -> None:
    calls = patch_get(
        lambda attempt, url, params: _response(200, "recovered")
        if attempt > 1
        else _response(503)
    )

    result = await _client().get_text("https://example.invalid")

    assert result == "recovered"
    assert len(calls) == 2


async def test_retries_are_bounded_by_max_retries(patch_get) -> None:
    calls = patch_get(lambda attempt, url, params: _response(502))

    with pytest.raises(PortalError):
        await _client(max_retries=2).get_text("https://example.invalid")

    # One initial attempt plus two retries.
    assert len(calls) == 3


async def test_a_timeout_is_retried_then_reported_as_a_timeout(
    patch_get, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    async def _get(self, url, params=None):  # noqa: ANN001
        calls.append(1)
        raise httpx.ConnectTimeout("too slow")

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    with pytest.raises(PortalTimeoutError) as exc_info:
        await _client(max_retries=1).get_text("https://example.invalid")

    assert len(calls) == 2
    assert exc_info.value.portal == "testportal"


async def test_a_client_error_is_not_retried(patch_get) -> None:
    # 404 is an answer, not a hiccup; retrying it just wastes the portal's time.
    calls = patch_get(lambda attempt, url, params: _response(404))

    with pytest.raises(PortalError):
        await _client().get_text("https://example.invalid")

    assert len(calls) == 1


# --- Refusals: never worked around ------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 407, 451])
async def test_a_refusal_is_terminal_and_never_retried(patch_get, status: int) -> None:
    """A portal saying no is respected, not retried around."""
    calls = patch_get(lambda attempt, url, params: _response(status))

    with pytest.raises(PortalBlockedError) as exc_info:
        await _client(max_retries=3).get_text("https://example.invalid")

    assert len(calls) == 1
    assert "bypass" in exc_info.value.message.lower()


async def test_rate_limiting_is_reported_not_retried(patch_get) -> None:
    calls = patch_get(lambda attempt, url, params: _response(429))

    with pytest.raises(PortalRateLimitedError):
        await _client(max_retries=3).get_text("https://example.invalid")

    # Hammering a portal that just asked us to slow down would be the opposite
    # of respecting the limit.
    assert len(calls) == 1


async def test_a_network_error_becomes_a_portal_error(monkeypatch) -> None:
    async def _get(self, url, params=None):  # noqa: ANN001
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    with pytest.raises(PortalError) as exc_info:
        await _client(max_retries=0).get_text("https://example.invalid")

    # Adapters must never leak a transport exception: the run records failures
    # from the portal taxonomy.
    assert exc_info.value.portal == "testportal"


async def test_head_ok_reports_false_instead_of_raising(monkeypatch) -> None:
    async def _head(self, url):  # noqa: ANN001
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx.AsyncClient, "head", _head)

    assert await _client().head_ok("https://example.invalid") is False
