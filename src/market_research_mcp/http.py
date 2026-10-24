"""Shared HTTP fetch: rate limiting, caching, retries, error mapping, redacted logging."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

from market_research_mcp import cache as cache_mod
from market_research_mcp.config import require
from market_research_mcp.errors import NotFound, ProviderDown, RateLimited
from market_research_mcp.logging_setup import redact
from market_research_mcp.models import utcnow
from market_research_mcp.ratelimit import limiter_for

__all__ = ["FetchResult", "aclose", "fetch", "get_client"]

logger = logging.getLogger(__name__)
# httpx logs full request URLs (with API keys) at INFO; keep it to warnings and above.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

Parse = Literal["json", "text", "bytes"]

TIMEOUT_SECONDS = 20.0
RETRIES = 2  # extra attempts after the first, for 5xx and connection errors
BACKOFF_SECONDS = (0.5, 1.5)
RATE_LIMIT_BACKOFF_SECONDS = 2.0
MAX_RETRY_AFTER_SECONDS = 10.0

# Replaced in tests to avoid real waiting.
_sleep = asyncio.sleep


@dataclass(frozen=True)
class FetchResult:
    data: Any
    url_redacted: str
    fetched_at: datetime
    from_cache: bool
    stale: bool


_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """One shared AsyncClient (20 s timeout; httpx decodes gzip automatically)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"Accept-Encoding": "gzip, deflate"},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _parse(raw: bytes, parse: Parse, source: str) -> Any:
    if parse == "bytes":
        return raw
    try:
        text = raw.decode("utf-8")
        return json.loads(text) if parse == "json" else text
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderDown(source, "unexpected response format") from exc


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


MAX_ERROR_BODY_CHARS = 500
_SECRET_PARAM = re.compile(r"key|token|secret|password|^t$", re.IGNORECASE)


def _error_body(resp: httpx.Response, url: str, params: dict[str, Any] | None) -> str:
    """Error response text for callers: request URL and secret param values removed."""
    try:
        text = resp.text
    except Exception:  # undecodable body
        return ""
    secrets = [str(v) for k, v in (params or {}).items() if _SECRET_PARAM.search(k) and str(v)]
    for needle in (str(httpx.URL(url, params=params)), url, *secrets):
        text = text.replace(needle, "")
    return redact(text)[:MAX_ERROR_BODY_CHARS]


async def fetch(
    source: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    cache_ns: str | None = None,
    cache_key: str | None = None,
    ttl: int | None = None,
    parse: Parse = "json",
    alternative: str | None = None,
) -> FetchResult:
    """GET `url` for `source` with caching, rate limiting and error mapping.

    Caching applies when both `cache_ns` and `cache_key` are given. `ttl=None` uses the
    namespace default from `cache.TTL_SECONDS`.
    Raises NotFound (404), RateLimited (429 with no cached copy), ProviderDown (other failures).
    """
    url_redacted = redact(str(httpx.URL(url, params=params)))
    use_cache = cache_ns is not None and cache_key is not None
    store = cache_mod.get_cache() if use_cache else None
    stale_hit = None

    if store is not None:
        fresh, stale_hit = store.get_fresh_or_stale(cache_ns, cache_key)  # type: ignore[arg-type]
        if fresh is not None:
            logger.debug("cache hit %s %s", source, url_redacted)
            raw = fresh.value if isinstance(fresh.value, bytes) else fresh.value.encode()
            return FetchResult(
                _parse(raw, parse, source), url_redacted, fresh.fetched_at, True, False
            )

    req_headers = dict(headers or {})
    if source == "edgar":
        req_headers["User-Agent"] = require("SEC_USER_AGENT")
        req_headers["Accept-Encoding"] = "gzip, deflate"

    client = get_client()
    limiter = limiter_for(source)
    attempt = 0
    rate_limited_once = False

    while True:
        await limiter.acquire()
        logger.info("GET %s %s", source, url_redacted)
        try:
            resp = await client.get(url, params=params, headers=req_headers)
        except httpx.TransportError as exc:
            if attempt < RETRIES:
                await _sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            logger.warning("%s transport error %s: %s", source, url_redacted, type(exc).__name__)
            raise ProviderDown(source, "connection failed", alternative) from None

        status = resp.status_code
        if status == 429:
            retry_after = _retry_after(resp)
            if not rate_limited_once:
                rate_limited_once = True
                wait = min(retry_after or RATE_LIMIT_BACKOFF_SECONDS, MAX_RETRY_AFTER_SECONDS)
                logger.warning("%s 429, backing off %.1fs: %s", source, wait, url_redacted)
                await _sleep(wait)
                continue
            if stale_hit is not None:
                logger.warning("%s 429 again, serving stale cache: %s", source, url_redacted)
                raw = (
                    stale_hit.value
                    if isinstance(stale_hit.value, bytes)
                    else stale_hit.value.encode()
                )
                return FetchResult(
                    _parse(raw, parse, source), url_redacted, stale_hit.fetched_at, True, True
                )
            raise RateLimited(source, retry_after)
        if status == 404:
            raise NotFound(url_redacted, source)
        if status >= 500:
            if attempt < RETRIES:
                await _sleep(BACKOFF_SECONDS[attempt])
                attempt += 1
                continue
            logger.warning("%s HTTP %d: %s", source, status, url_redacted)
            raise ProviderDown(source, f"HTTP {status}", alternative)
        if status >= 400:
            logger.warning("%s HTTP %d: %s", source, status, url_redacted)
            body = _error_body(resp, url, params)
            raise ProviderDown(source, f"HTTP {status}", alternative, body=body)
        break

    raw = resp.content
    data = _parse(raw, parse, source)
    fetched_at = utcnow()
    if store is not None:
        effective_ttl = ttl if ttl is not None else cache_mod.ttl_for(cache_ns)  # type: ignore[arg-type]
        store.set(cache_ns, cache_key, raw, effective_ttl, now=fetched_at)  # type: ignore[arg-type]
    return FetchResult(data, url_redacted, fetched_at, False, False)
