"""Latency targets from plans/01: under 20 s cold (empty cache), under 5 s warm.

Live only: real network and keys. CI runs `-m "not live"`, so this never runs there.
Run locally with the keys file pointed to by MRM_ENV_FILE:

    MRM_ENV_FILE=/path/to/.env uv run pytest -m latency -s

Each tool is timed cold with its own empty cache, then warm against that same cache.
Timings are printed (no keys or other values are printed).
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from market_research_mcp import cache as cache_mod
from market_research_mcp import config, http
from market_research_mcp.cache import Cache
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.server import build_server

pytestmark = [pytest.mark.live, pytest.mark.latency]

COLD_LIMIT_S = 20.0
WARM_LIMIT_S = 5.0
TICKER = "MSFT"


@pytest.fixture
def live_env(monkeypatch):
    """Load the real keys from MRM_ENV_FILE (or the usual .env files) and restore env after."""
    saved = dict(os.environ)
    config.load_env_files()
    config.get_settings.cache_clear()
    if not os.environ.get("SEC_USER_AGENT"):
        pytest.skip("SEC_USER_AGENT not set; point MRM_ENV_FILE at a .env with real keys")
    yield
    os.environ.clear()
    os.environ.update(saved)
    config.get_settings.cache_clear()


async def _latest_10k_accession(mcp) -> str:
    _, out = await mcp.call_tool("list_filings", {"ticker": TICKER, "forms": ["10-K"], "limit": 1})
    return out["items"][0]["accession"]


CASES: dict[str, dict[str, Any] | None] = {
    "resolve_ticker": {"query": TICKER},
    "get_fundamentals": {"ticker": TICKER},
    "get_key_ratios": {"ticker": TICKER},
    "get_price_stats": {"ticker": TICKER},
    "get_filing_section": None,  # accession looked up live
}


async def _timed(mcp, name: str, args: dict[str, Any]) -> float:
    start = time.perf_counter()
    _, out = await mcp.call_tool(name, args)
    elapsed = time.perf_counter() - start
    assert out, f"{name} returned nothing"
    return elapsed


async def _reset(store: Cache | None) -> None:
    await http.aclose()
    reset_limiters()
    cache_mod.set_default_cache(store)


@pytest.mark.parametrize("name", list(CASES))
async def test_cold_and_warm_latency(name, live_env, tmp_path):
    mcp = build_server()
    args = CASES[name]
    if args is None:
        await _reset(Cache(tmp_path / "lookup.db"))
        args = {"ticker": TICKER, "accession": await _latest_10k_accession(mcp), "section": "1A"}

    store = Cache(tmp_path / "cache.db")  # empty: cold
    try:
        await _reset(store)
        cold = await _timed(mcp, name, args)
        await http.aclose()
        warm = await _timed(mcp, name, args)
    finally:
        await _reset(None)

    print(f"\nLATENCY {name}: cold {cold:.2f} s, warm {warm:.2f} s")
    assert cold < COLD_LIMIT_S, f"{name} cold {cold:.2f} s >= {COLD_LIMIT_S} s"
    assert warm < WARM_LIMIT_S, f"{name} warm {warm:.2f} s >= {WARM_LIMIT_S} s"
