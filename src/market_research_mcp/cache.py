"""SQLite cache for provider responses. Expired entries are kept so callers can serve stale data.

Each operation opens a short-lived connection, so the cache is safe to use from threads.
Operations are small and fast; callers in hot async paths may wrap them in asyncio.to_thread.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from market_research_mcp.models import utcnow

__all__ = [
    "TTL_SECONDS",
    "UNTIL_NEXT_CLOSE",
    "Cache",
    "CacheHit",
    "get_cache",
    "seconds_until_next_us_close",
    "set_default_cache",
    "ttl_for",
]

NEW_YORK = ZoneInfo("America/New_York")

UNTIL_NEXT_CLOSE = "until_next_us_close"
"""Sentinel TTL: expire 30 minutes after the next US market close (16:00 New York)."""

TTL_SECONDS: dict[str, int | str | None] = {
    "tickers": 7 * 24 * 3600,
    "submissions": 6 * 3600,
    "companyfacts": 24 * 3600,
    "filing_docs": None,  # forever: filed documents never change
    "prices": UNTIL_NEXT_CLOSE,
    "news": 3600,
    "fred": 24 * 3600,
    "calendar": 12 * 3600,
}

DEFAULT_TTL_SECONDS = 3600


def seconds_until_next_us_close(now: datetime | None = None) -> int:
    """Seconds until the next weekday 16:30 New York time (close + 30 min) after `now`.

    US market holidays are not considered; on a holiday the entry simply expires early.
    """
    now = now or utcnow()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(NEW_YORK)
    target = local.replace(hour=16, minute=30, second=0, microsecond=0)
    while target <= local or target.weekday() >= 5:
        target = (target + timedelta(days=1)).replace(hour=16, minute=30)
    # Same-tzinfo subtraction ignores DST offsets, so subtract in UTC.
    return max(1, int((target.astimezone(UTC) - now.astimezone(UTC)).total_seconds()))


def ttl_for(namespace: str, now: datetime | None = None) -> int | None:
    """Resolve the TTL in seconds for a namespace (None = never expires)."""
    ttl = TTL_SECONDS.get(namespace, DEFAULT_TTL_SECONDS)
    if ttl == UNTIL_NEXT_CLOSE:
        return seconds_until_next_us_close(now)
    return ttl  # type: ignore[return-value]


@dataclass(frozen=True)
class CacheHit:
    value: bytes | str
    fetched_at: datetime
    expires_at: datetime | None
    expired: bool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value BLOB NOT NULL,
    is_text INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (namespace, key)
)
"""


class Cache:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def get(self, namespace: str, key: str, now: datetime | None = None) -> CacheHit | None:
        """Return the entry (fresh or expired, see `expired`) or None if absent."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value, is_text, fetched_at, expires_at FROM cache "
                "WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        raw, is_text, fetched_at, expires_at = row
        value: bytes | str = bytes(raw).decode("utf-8") if is_text else bytes(raw)
        exp = datetime.fromisoformat(expires_at) if expires_at else None
        now = now or utcnow()
        return CacheHit(
            value=value,
            fetched_at=datetime.fromisoformat(fetched_at),
            expires_at=exp,
            expired=exp is not None and now >= exp,
        )

    def get_fresh_or_stale(
        self, namespace: str, key: str, now: datetime | None = None
    ) -> tuple[CacheHit | None, CacheHit | None]:
        """Return (fresh, stale): at most one is set. Serve `fresh` directly; keep `stale`
        as a fallback when the provider rate-limits (429)."""
        hit = self.get(namespace, key, now)
        if hit is None:
            return None, None
        return (None, hit) if hit.expired else (hit, None)

    def set(
        self,
        namespace: str,
        key: str,
        value: bytes | str,
        ttl_seconds: int | None,
        now: datetime | None = None,
    ) -> None:
        """Store a value. `ttl_seconds=None` keeps it forever."""
        now = now or utcnow()
        expires = now + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
        is_text = isinstance(value, str)
        blob = value.encode("utf-8") if is_text else value
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        namespace,
                        key,
                        blob,
                        int(is_text),
                        now.isoformat(),
                        expires.isoformat() if expires else None,
                    ),
                )
        finally:
            conn.close()

    def delete(self, namespace: str, key: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM cache WHERE namespace = ? AND key = ?", (namespace, key))
        finally:
            conn.close()


_default: Cache | None = None


def get_cache() -> Cache:
    """Process-wide cache at `settings.cache_path`."""
    global _default
    if _default is None:
        from market_research_mcp.config import get_settings

        _default = Cache(get_settings().cache_path)
    return _default


def set_default_cache(cache: Cache | None) -> None:
    """Replace (or reset with None) the process-wide cache. Used by tests."""
    global _default
    _default = cache
