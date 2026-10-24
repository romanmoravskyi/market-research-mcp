"""Async token-bucket rate limiting per data source."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

__all__ = ["CompositeLimiter", "RateLimiter", "limiter_for", "reset_limiters"]


class RateLimiter:
    """Token bucket: `rate` tokens per `per_seconds`, holding at most `burst` tokens."""

    def __init__(
        self,
        rate: float,
        per_seconds: float,
        burst: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0 or per_seconds <= 0:
            raise ValueError("rate and per_seconds must be positive")
        self.fill_rate = rate / per_seconds  # tokens per second
        self.capacity = float(burst if burst is not None else rate)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock: asyncio.Lock | None = None

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.fill_rate)
        self._updated = now

    def wait_time(self) -> float:
        """Seconds until one token is available (0 if available now)."""
        self._refill()
        return 0.0 if self._tokens >= 1 else (1 - self._tokens) / self.fill_rate

    def try_acquire(self) -> bool:
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def acquire(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            while not self.try_acquire():
                await self._sleep(self.wait_time())


class CompositeLimiter:
    """Several buckets that must all allow a request (e.g. per-hour AND per-day caps)."""

    def __init__(self, *limiters: RateLimiter) -> None:
        self.limiters = limiters

    async def acquire(self) -> None:
        # Take tokens only when every bucket has one, so one bucket never leaks tokens.
        while True:
            waits = [lim.wait_time() for lim in self.limiters]
            longest = max(waits)
            if longest <= 0:
                for lim in self.limiters:
                    lim.try_acquire()
                return
            await self.limiters[0]._sleep(longest)


class _NoLimit:
    async def acquire(self) -> None:
        return None


def _build(source: str) -> RateLimiter | CompositeLimiter | _NoLimit:
    match source:
        case "edgar":
            return RateLimiter(10, 1)
        case "fred":
            return RateLimiter(120, 60)
        case "tiingo":
            return CompositeLimiter(RateLimiter(50, 3600), RateLimiter(1000, 86400))
        case "finnhub":
            return RateLimiter(60, 60)
        case _:
            return _NoLimit()


_registry: dict[str, RateLimiter | CompositeLimiter | _NoLimit] = {}


def limiter_for(source: str) -> RateLimiter | CompositeLimiter | _NoLimit:
    """Shared limiter for a source. Unknown sources are not limited."""
    if source not in _registry:
        _registry[source] = _build(source)
    return _registry[source]


def reset_limiters() -> None:
    """Drop all limiters (tests; also needed if the event loop changes)."""
    _registry.clear()
