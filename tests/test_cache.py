from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from market_research_mcp.cache import (
    TTL_SECONDS,
    Cache,
    seconds_until_next_us_close,
    ttl_for,
)


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "sub" / "cache.db")


def ny(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/New_York"))


def test_creates_parent_dir(tmp_path):
    Cache(tmp_path / "a" / "b" / "c.db")
    assert (tmp_path / "a" / "b" / "c.db").exists()


def test_miss_returns_none(cache):
    assert cache.get("tickers", "AAPL") is None
    assert cache.get_fresh_or_stale("tickers", "AAPL") == (None, None)


def test_roundtrip_bytes_and_str(cache):
    cache.set("tickers", "b", b"\x00\x01", 60)
    cache.set("tickers", "s", "héllo", 60)
    assert cache.get("tickers", "b").value == b"\x00\x01"
    assert cache.get("tickers", "s").value == "héllo"


def test_ttl_expiry_keeps_stale(cache):
    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    cache.set("news", "k", "v", 3600, now=t0)
    hit = cache.get("news", "k", now=t0 + timedelta(minutes=59))
    assert not hit.expired and hit.fetched_at == t0
    later = t0 + timedelta(hours=1, seconds=1)
    hit = cache.get("news", "k", now=later)
    assert hit.expired and hit.value == "v"
    fresh, stale = cache.get_fresh_or_stale("news", "k", now=later)
    assert fresh is None and stale.value == "v"
    fresh, stale = cache.get_fresh_or_stale("news", "k", now=t0)
    assert fresh.value == "v" and stale is None


def test_none_ttl_is_forever(cache):
    t0 = datetime(2020, 1, 1, tzinfo=UTC)
    cache.set("filing_docs", "k", "v", None, now=t0)
    hit = cache.get("filing_docs", "k", now=t0 + timedelta(days=3650))
    assert not hit.expired and hit.expires_at is None


def test_namespaces_are_separate(cache):
    cache.set("a", "k", "1", 60)
    assert cache.get("b", "k") is None


def test_overwrite_and_delete(cache):
    cache.set("a", "k", "1", 60)
    cache.set("a", "k", "2", 60)
    assert cache.get("a", "k").value == "2"
    cache.delete("a", "k")
    assert cache.get("a", "k") is None


def test_ttl_constants():
    assert TTL_SECONDS["tickers"] == 7 * 86400
    assert TTL_SECONDS["submissions"] == 6 * 3600
    assert TTL_SECONDS["companyfacts"] == 86400
    assert TTL_SECONDS["filing_docs"] is None
    assert TTL_SECONDS["news"] == 3600
    assert TTL_SECONDS["fred"] == 86400
    assert TTL_SECONDS["calendar"] == 12 * 3600
    assert ttl_for("filing_docs") is None
    assert ttl_for("tickers") == 7 * 86400


def test_until_close_same_day():
    # Friday 2026-10-23 10:00 New York -> 16:30 same day
    assert seconds_until_next_us_close(ny(2026, 10, 23, 10, 0)) == 6.5 * 3600


def test_until_close_after_close_rolls_to_next_weekday():
    # Friday 17:00 -> Monday 16:30
    assert seconds_until_next_us_close(ny(2026, 10, 23, 17, 0)) == (2 * 24 + 23.5) * 3600


def test_until_close_weekend():
    # Saturday 12:00 -> Monday 16:30
    assert seconds_until_next_us_close(ny(2026, 10, 24, 12, 0)) == (2 * 24 + 4.5) * 3600


def test_until_close_across_dst_change():
    # US DST ends Sunday 2026-11-01: Friday 17:00 EDT -> Monday 16:30 EST is 1 h longer.
    assert seconds_until_next_us_close(ny(2026, 10, 30, 17, 0)) == (2 * 24 + 23.5 + 1) * 3600


def test_until_close_accepts_utc_and_rejects_naive():
    utc = ny(2026, 10, 23, 10, 0).astimezone(UTC)
    assert seconds_until_next_us_close(utc) == 6.5 * 3600
    assert ttl_for("prices", utc) == 6.5 * 3600
    with pytest.raises(ValueError):
        seconds_until_next_us_close(datetime(2026, 1, 1))
