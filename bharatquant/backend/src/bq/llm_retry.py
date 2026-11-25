"""Retry classification and a sliding-window rate limiter.

Adapted from Agent Zero's ``helpers/rate_limiter.py`` and the transient-error
classification in ``helpers/litellm_transport.py`` (MIT License, Copyright (c)
2025 Agent Zero, s.r.o, https://github.com/agent0ai/agent-zero).

Reimplemented against our ``httpx``-based clients, which raise
``httpx.HTTPStatusError`` / ``httpx.HTTPError`` rather than OpenAI/LiteLLM
exception chains. The *policy* is the same: retry timeouts, rate limits and 5xx;
never retry a 4xx the caller must fix; never retry after partial output has
already been consumed.
"""

import time
from collections.abc import Callable, Mapping

import httpx

# Status codes worth retrying. 408 timeout, 429 rate limit, 5xx upstream.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 1.5
MAX_DELAY_SECONDS = 30.0


class RetryExhausted(ValueError):
    pass


def is_transient(exc: BaseException) -> bool:
    """Whether a failed provider call is worth retrying."""
    status: int | None = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    elif isinstance(exc, httpx.TimeoutException):
        return True
    elif isinstance(exc, httpx.TransportError):
        # Connection resets, DNS blips, read errors on the wire.
        return True
    if status is not None:
        if status in TRANSIENT_STATUS:
            return True
        if 500 <= status < 600:
            return True
        return False
    return False


def backoff_delay(attempt: int, *, base: float = DEFAULT_BASE_DELAY_SECONDS) -> float:
    """Exponential backoff capped at MAX_DELAY_SECONDS. ``attempt`` is 1-based."""
    if attempt < 1:
        attempt = 1
    growth = float(2 ** (attempt - 1))
    return min(MAX_DELAY_SECONDS, float(base) * growth)


def run_with_retry(
    operation: Callable[[], "object"],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    should_retry: Callable[[BaseException], bool] = is_transient,
) -> object:
    """Call ``operation`` with bounded retries on transient failures.

    Returns the operation's value, or re-raises the last error once attempts are
    exhausted. Non-transient errors propagate immediately.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1.")
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            last = exc
            if attempt >= max_attempts or not should_retry(exc):
                raise
            sleep(backoff_delay(attempt, base=base_delay))
    assert last is not None
    raise RetryExhausted(str(last)) from last


class RateLimiter:
    """Sliding-window limiter over named counters.

    Ported from Agent Zero's ``RateLimiter``. Kept synchronous and dependency
    free; callers can wrap it in their own scheduling. ``add`` records usage,
    ``would_exceed`` reports whether the next unit fits, ``wait_seconds`` gives
    the time until the oldest unit ages out of the window.
    """

    def __init__(self, timeframe_seconds: float, limits: Mapping[str, int]) -> None:
        if timeframe_seconds <= 0:
            raise ValueError("Rate-limit timeframe must be positive.")
        self.timeframe = float(timeframe_seconds)
        self.limits = {key: int(value) for key, value in limits.items()}
        if any(value < 0 for value in self.limits.values()):
            raise ValueError("Rate-limit values must be nonnegative.")
        self._events: dict[str, list[tuple[float, int]]] = {
            key: [] for key in self.limits
        }

    def _prune(self, key: str, now: float) -> None:
        cutoff = now - self.timeframe
        self._events[key] = [
            (stamp, amount) for stamp, amount in self._events[key] if stamp > cutoff
        ]

    def add(self, now: float | None = None, **amounts: int) -> None:
        now = time.monotonic() if now is None else now
        for key, amount in amounts.items():
            if key not in self._events:
                self._events[key] = []
            self._events[key].append((now, int(amount)))

    def total(self, key: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        if key not in self._events:
            return 0
        self._prune(key, now)
        return sum(amount for _, amount in self._events[key])

    def would_exceed(self, now: float | None = None, **amounts: int) -> bool:
        now = time.monotonic() if now is None else now
        for key, amount in amounts.items():
            limit = self.limits.get(key, 0)
            if limit <= 0:
                continue
            if self.total(key, now) + int(amount) > limit:
                return True
        return False

    def wait_seconds(self, now: float | None = None, **amounts: int) -> float:
        """Seconds until ``amounts`` would fit, or 0 if it already does."""
        now = time.monotonic() if now is None else now
        longest = 0.0
        for key, amount in amounts.items():
            limit = self.limits.get(key, 0)
            if limit <= 0:
                continue
            self._prune(key, now)
            events = list(self._events[key])
            projected = sum(value for _, value in events) + int(amount)
            while events and projected > limit:
                stamp, value = events.pop(0)
                projected -= value
                longest = max(longest, (stamp + self.timeframe) - now)
        return max(0.0, longest)
