from datetime import datetime

import pytest

from market_research_mcp.config import ConfigError, load_settings, require
from market_research_mcp.logging_setup import redact
from market_research_mcp.server import build_server


async def test_ping_registered_and_returns_fields():
    mcp = build_server()
    tools = {t.name for t in await mcp.list_tools()}
    assert "ping" in tools

    _, structured = await mcp.call_tool("ping", {})
    result = structured.get("result", structured)
    assert result["status"] == "ok"
    assert result["server"] == "market-research"
    assert result["version"]
    assert datetime.fromisoformat(result["retrieved_at"]).tzinfo is not None


EXPECTED_TOOLS = {
    "ping",
    "resolve_ticker",
    "list_filings",
    "get_filing_section",
    "get_filing_exhibit",
    "get_insider_transactions",
    "get_fundamentals",
    "get_price_history",
    "get_price_stats",
    "get_earnings_calendar",
    "get_macro_series",
}


async def test_full_tool_set_registered():
    mcp = build_server()
    tools = {t.name for t in await mcp.list_tools()}
    assert EXPECTED_TOOLS <= tools, f"missing: {sorted(EXPECTED_TOOLS - tools)}"


def test_require_raises_naming_var(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="FRED_API_KEY"):
        require("FRED_API_KEY")


def test_require_returns_value(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "abc")
    assert require("FRED_API_KEY") == "abc"


def test_missing_keys_do_not_crash(monkeypatch):
    for name in ("SEC_USER_AGENT", "FRED_API_KEY", "TIINGO_API_KEY", "PRICE_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings(dotenv=False)
    assert settings.fred_api_key is None
    assert settings.price_provider == "tiingo"


@pytest.mark.parametrize("key", ["token", "key", "api_key", "apikey", "t", "API_KEY"])
def test_redact_masks_secrets(key):
    url = f"https://api.example.com/x?symbol=AAPL&{key}=SECRET123&format=json"
    out = redact(url)
    assert "SECRET123" not in out
    assert "symbol=AAPL" in out
    assert "format=json" in out


def test_redact_leaves_other_params():
    assert redact("https://x.test/?ticker=MSFT") == "https://x.test/?ticker=MSFT"
