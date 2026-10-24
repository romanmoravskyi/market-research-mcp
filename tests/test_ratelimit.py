import pytest

from market_research_mcp.ratelimit import (
    CompositeLimiter,
    RateLimiter,
    limiter_for,
    reset_limiters,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def make(rate, per, burst=None, *, clock):
    return RateLimiter(rate, per, burst, clock=clock, sleep=clock.sleep)


async def test_burst_then_waits():
    clock = FakeClock()
    lim = make(10, 1, clock=clock)
    for _ in range(10):
        await lim.acquire()
    assert clock.sleeps == []
    await lim.acquire()
    assert clock.t == pytest.approx(0.1)


def test_refills_over_time():
    clock = FakeClock()
    lim = make(2, 1, burst=2, clock=clock)
    assert lim.try_acquire() and lim.try_acquire()
    assert not lim.try_acquire()
    clock.t += 0.5
    assert lim.try_acquire()


async def test_custom_burst():
    clock = FakeClock()
    lim = make(60, 60, burst=1, clock=clock)
    await lim.acquire()
    await lim.acquire()
    assert clock.t == pytest.approx(1.0)


async def test_composite_respects_tightest_bucket():
    clock = FakeClock()
    hourly = make(50, 3600, clock=clock)
    daily = make(3, 86400, clock=clock)
    comp = CompositeLimiter(hourly, daily)
    for _ in range(3):
        await comp.acquire()
    assert clock.t == 0
    await comp.acquire()
    assert clock.t == pytest.approx(86400 / 3)


def test_invalid_rate():
    with pytest.raises(ValueError):
        RateLimiter(0, 1)


def test_registry_preconfigured():
    reset_limiters()
    edgar = limiter_for("edgar")
    assert isinstance(edgar, RateLimiter)
    assert edgar.fill_rate == 10 and edgar.capacity == 10
    assert limiter_for("edgar") is edgar
    assert limiter_for("fred").fill_rate == pytest.approx(2.0)
    assert limiter_for("finnhub").fill_rate == pytest.approx(1.0)
    tiingo = limiter_for("tiingo")
    assert isinstance(tiingo, CompositeLimiter)
    assert sorted(lim.fill_rate for lim in tiingo.limiters) == pytest.approx(
        sorted([50 / 3600, 1000 / 86400])
    )
    assert sorted(lim.capacity for lim in tiingo.limiters) == [50, 1000]
    reset_limiters()


async def test_unknown_source_not_limited():
    await limiter_for("something-else").acquire()
