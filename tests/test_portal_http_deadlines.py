"""The shared HTTP layer under a search deadline.

Why this exists
---------------
An adapter checks its deadline *between* requests. Only the layer that starts a
request can stop one that will outlive the ceiling, and until it did, a single
call could legitimately run past the ceiling meant to bound it: measured under
the default policy, 30s per attempt x 3 attempts + 6s backoff + 6s rate-limit
spacing = 102s against a 90s query timeout. The caller's ``asyncio.wait_for``
then cancelled the coroutine, and every listing collected so far died with it,
because they exist only in that coroutine's local variables.

So these tests pin one property: **the clamp may only ever shorten an attempt.**
A deadline can take time away; it can never grant more than the portal's own
configured policy allows, and it can never buy a shorter rate-limit interval.

All transport is stubbed; nothing here makes a real request.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.portals.base import Deadline
from app.portals.exceptions import PortalError, PortalTimeoutError
from app.portals.http import (
    MIN_ATTEMPT_SECONDS,
    HttpPolicy,
    PortalHttpClient,
    active_deadline,
    use_deadline,
)


class ScriptedDeadline(Deadline):
    """A deadline whose remaining time is dictated rather than measured.

    Real clocks make these assertions flaky for no gain: what is under test is
    the arithmetic and the branch it drives, not the ability to tell the time.
    The final reading repeats, so a test scripts only the readings it cares
    about.
    """

    def __init__(self, readings: list[float]) -> None:
        super().__init__(expires_at=0.0)
        object.__setattr__(self, "_readings", list(readings))

    @property
    def remaining(self) -> float:
        readings = self._readings  # type: ignore[attr-defined]
        return readings.pop(0) if len(readings) > 1 else readings[0]


class CollapsingDeadline(Deadline):
    """Generous until ``after_calls`` requests have been sent, then spent.

    Scripting these by reading order was brittle - ``_send`` consults the
    deadline several times per attempt, and adding one consultation would have
    quietly broken tests that are not about consultations at all. This says
    what the tests actually mean: there was time for the first request and
    there is none for the next.
    """

    def __init__(
        self,
        calls: list[str],
        *,
        before: float = 30.0,
        after: float = 0.0,
        after_calls: int = 1,
    ) -> None:
        super().__init__(expires_at=0.0)
        object.__setattr__(self, "_calls", calls)
        object.__setattr__(self, "_before", before)
        object.__setattr__(self, "_after", after)
        object.__setattr__(self, "_after_calls", after_calls)

    @property
    def remaining(self) -> float:
        if len(self._calls) >= self._after_calls:  # type: ignore[attr-defined]
            return self._after  # type: ignore[attr-defined]
        return self._before  # type: ignore[attr-defined]


def _client(**overrides) -> PortalHttpClient:
    defaults = {
        "timeout_seconds": 30.0,
        "max_retries": 2,
        "retry_backoff_seconds": 0.0,
        "rate_limit_per_minute": 0,
    }
    return PortalHttpClient(
        portal="testportal", policy=HttpPolicy(**{**defaults, **overrides})
    )


def _response(status: int, text: str = "ok") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=text,
        request=httpx.Request("GET", "https://example.invalid"),
    )


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """Record every attempt's configured timeout and serve canned responses."""

    def _apply(statuses: list[int]):
        timeouts: list[float] = []
        calls: list[str] = []
        original_init = httpx.AsyncClient.__init__

        def _init(self, *args, **kwargs):  # noqa: ANN001
            timeout = kwargs.get("timeout")
            timeouts.append(
                timeout.read if isinstance(timeout, httpx.Timeout) else timeout
            )
            return original_init(self, *args, **kwargs)

        async def _get(self, url, params=None):  # noqa: ANN001
            calls.append(url)
            index = min(len(calls) - 1, len(statuses) - 1)
            return _response(statuses[index])

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
        monkeypatch.setattr(httpx.AsyncClient, "get", _get)
        return timeouts, calls

    return _apply


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record what the client waited for instead of actually waiting."""
    waits: list[float] = []

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("app.portals.http.asyncio.sleep", _sleep)
    return waits


# --- The context variable ---------------------------------------------------


def test_no_deadline_is_set_by_default() -> None:
    assert active_deadline() is None


def test_a_deadline_is_restored_after_the_block_it_governs() -> None:
    outer = Deadline.in_seconds(10.0)
    with use_deadline(outer):
        assert active_deadline() is outer
        with use_deadline(Deadline.in_seconds(1.0)):
            assert active_deadline() is not outer
        assert active_deadline() is outer
    assert active_deadline() is None


def test_a_deadline_is_restored_even_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), use_deadline(Deadline.in_seconds(10.0)):
        raise RuntimeError("boom")
    assert active_deadline() is None


# --- Clamping ---------------------------------------------------------------


async def test_the_attempt_timeout_is_clamped_to_the_remaining_deadline(
    transport,
) -> None:
    timeouts, calls = transport([200])
    client = _client(timeout_seconds=30.0)

    with use_deadline(ScriptedDeadline([5.0])):
        await client.get_text("https://example.invalid/x")

    assert calls == ["https://example.invalid/x"]
    assert timeouts == [5.0]


async def test_the_policy_timeout_still_wins_when_it_is_the_tighter_of_the_two(
    transport,
) -> None:
    """The clamp takes time away and never grants it: a portal configured for
    one second does not get sixty because the leg happens to have them.
    """
    timeouts, _ = transport([200])
    client = _client(timeout_seconds=1.0)

    with use_deadline(ScriptedDeadline([60.0])):
        await client.get_text("https://example.invalid/x")

    assert timeouts == [1.0]


async def test_without_a_deadline_the_configured_timeout_is_used_unchanged(
    transport,
) -> None:
    """The regression guard for every existing caller: no deadline, no change."""
    timeouts, calls = transport([200])
    client = _client(timeout_seconds=30.0)

    await client.get_text("https://example.invalid/x")

    assert calls == ["https://example.invalid/x"]
    assert timeouts == [30.0]


async def test_a_timeout_message_reports_the_time_actually_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get(self, url, params=None):  # noqa: ANN001
        raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    client = _client(timeout_seconds=30.0, max_retries=0)

    with pytest.raises(PortalTimeoutError) as caught:  # noqa: PT012
        with use_deadline(ScriptedDeadline([4.0])):
            await client.get_text("https://example.invalid/x")

    assert "4s" in str(caught.value)


# --- Not starting what cannot finish ----------------------------------------


async def test_nothing_is_sent_when_the_deadline_has_already_expired(
    transport,
) -> None:
    _, calls = transport([200])
    client = _client()

    with pytest.raises(PortalTimeoutError):  # noqa: PT012
        with use_deadline(ScriptedDeadline([0.0])):
            await client.get_text("https://example.invalid/x")

    assert calls == []


async def test_an_attempt_too_short_to_succeed_is_not_started(transport) -> None:
    """Below the floor there is not enough left to connect, let alone be
    answered, so starting one would only burn the margin the adapter needs to
    return under its own power.
    """
    _, calls = transport([200])
    client = _client()

    with pytest.raises(PortalTimeoutError):  # noqa: PT012
        with use_deadline(ScriptedDeadline([MIN_ATTEMPT_SECONDS / 2])):
            await client.get_text("https://example.invalid/x")

    assert calls == []


async def test_a_retry_is_not_started_once_the_deadline_has_gone(
    transport, slept
) -> None:
    """503 is retryable and the policy allows three attempts. Only the first
    fits inside the deadline, so only the first happens.
    """
    _, calls = transport([503])
    client = _client(max_retries=2, retry_backoff_seconds=0.0)

    with pytest.raises(PortalError):  # noqa: PT012
        with use_deadline(CollapsingDeadline(calls)):
            await client.get_text("https://example.invalid/x")

    assert len(calls) == 1


async def test_all_three_attempts_still_run_when_the_deadline_is_generous(
    transport, slept
) -> None:
    """The other half of the previous test: the clamp must not cost a retry
    there was time for.
    """
    _, calls = transport([503])
    client = _client(max_retries=2, retry_backoff_seconds=0.0)

    with pytest.raises(PortalError):  # noqa: PT012
        with use_deadline(ScriptedDeadline([600.0])):
            await client.get_text("https://example.invalid/x")

    assert len(calls) == 3


# --- Waiting ----------------------------------------------------------------


async def test_retry_backoff_is_not_slept_through_past_the_deadline(
    transport, slept
) -> None:
    """A ten-second backoff with two seconds left would wake only to be
    cancelled. The backoff is not shortened either: it exists to spare a
    struggling portal, not to be traded away for our own schedule.
    """
    _, calls = transport([503])
    client = _client(max_retries=2, retry_backoff_seconds=10.0)

    with pytest.raises(PortalError):  # noqa: PT012
        with use_deadline(CollapsingDeadline(calls, before=30.0, after=2.0)):
            await client.get_text("https://example.invalid/x")

    assert len(calls) == 1
    assert slept == []


async def test_backoff_is_still_served_in_full_when_it_fits(transport, slept) -> None:
    _, calls = transport([503])
    client = _client(max_retries=1, retry_backoff_seconds=3.0)

    with pytest.raises(PortalError):  # noqa: PT012
        with use_deadline(ScriptedDeadline([600.0])):
            await client.get_text("https://example.invalid/x")

    assert len(calls) == 2
    assert slept == [3.0]


async def test_a_rate_limit_slot_beyond_the_deadline_sends_nothing(
    transport, slept
) -> None:
    """One request per minute, and five seconds left. Waiting less than the
    configured interval would be sending faster than the portal allows, so the
    attempt is abandoned instead: a deadline must never buy throughput.
    """
    _, calls = transport([503])
    client = _client(
        max_retries=2, retry_backoff_seconds=0.0, rate_limit_per_minute=1
    )

    with pytest.raises(PortalError):  # noqa: PT012
        with use_deadline(CollapsingDeadline(calls, before=50.0, after=5.0)):
            await client.get_text("https://example.invalid/x")

    assert len(calls) == 1
    # The sixty-second interval was never waited, and never shortened either.
    assert max(slept, default=0.0) < 60.0


async def test_a_retry_after_wait_is_not_served_past_the_deadline(
    monkeypatch: pytest.MonkeyPatch, slept
) -> None:
    """The portal asked for twenty seconds and three are left. We stop rather
    than wait, and we never wait less than it asked for.
    """
    calls: list[str] = []

    async def _get(self, url, params=None):  # noqa: ANN001
        calls.append(url)
        return httpx.Response(
            status_code=429,
            text="slow down",
            headers={"Retry-After": "20"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    client = _client(max_retries=2)

    with pytest.raises(PortalError):  # noqa: PT012
        with use_deadline(CollapsingDeadline(calls, before=30.0, after=3.0)):
            await client.get_text("https://example.invalid/x")

    assert len(calls) == 1
    assert slept == []


# --- The whole attempt, not just each socket operation ----------------------


async def test_a_request_that_never_stalls_is_still_stopped_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx timeouts are per operation, not per request.

    A body that keeps arriving never trips the read timeout, so a large
    download can outlive any value passed to the client. Measured on Lever,
    whose board feeds run to 8MB, clamping alone still let a request reach the
    query ceiling and be cancelled from outside - which is what destroys the
    adapter's collected listings. The whole attempt has to be bounded.
    """
    started = 0

    async def _slow_but_never_idle(self, url, params=None):  # noqa: ANN001
        nonlocal started
        started += 1
        # Never raises a transport timeout: this is a healthy, steady download
        # that simply takes longer than the leg has left.
        await asyncio.sleep(30.0)
        raise AssertionError("the deadline should have stopped this")

    monkeypatch.setattr(httpx.AsyncClient, "get", _slow_but_never_idle)
    client = _client(timeout_seconds=30.0, max_retries=0)

    with pytest.raises(PortalTimeoutError) as caught:  # noqa: PT012
        with use_deadline(ScriptedDeadline([0.75])):
            await client.get_text("https://example.invalid/big")

    assert started == 1
    assert "deadline" in str(caught.value)


async def test_an_unbounded_call_is_left_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no deadline there is no total bound, only the policy's own
    per-operation timeout - unchanged for every existing caller.
    """
    seen: list[float | None] = []

    async def _get(self, url, params=None):  # noqa: ANN001
        seen.append(self.timeout.read)
        return _response(200)

    monkeypatch.setattr(httpx.AsyncClient, "get", _get)
    client = _client(timeout_seconds=30.0)

    body = await client.get_text("https://example.invalid/x")

    assert body == "ok"
    assert seen == [30.0]
