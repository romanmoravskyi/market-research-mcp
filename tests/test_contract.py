"""Contract tests: every registered tool and prompt, called in-process against recorded fixtures.

For each tool the test checks the declared interface (description, JSON input schema, output
schema), calls it through FastMCP with every outbound HTTP request served by respx (no network),
validates the structured result against the declared output schema, and checks provenance:
every number sits inside an item carrying source, source_url, retrieved_at and as_of (or a
null as_of with a reason in notes), and no source_url leaks a key, token or User-Agent.

The tool list is read from the server at collection time. A newly registered tool without an
entry in CASES fails `test_tool_contract`.
"""

from __future__ import annotations

import gzip
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
import respx

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.config import Settings
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.server import build_server
from market_research_mcp.sources import calendar, edgar, prices, xbrl

FIXTURES = Path(__file__).parent / "fixtures"
SEC = FIXTURES / "sec"
DOCS = SEC / "docs"
FORM4 = SEC / "form4"
MANIFEST = json.loads((DOCS / "manifest.json").read_text(encoding="utf-8"))
TENK = MANIFEST["tenk"]
EIGHTK = MANIFEST["eightk"]
MSFT_CIK = "789019"
ARCHIVE = f"https://www.sec.gov/Archives/edgar/data/{MSFT_CIK}/"
SUB = "https://data.sec.gov/submissions/"
FRED_SERIES_URL = "https://api.stlouisfed.org/fred/series"
FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
TODAY = date(2026, 10, 24)
UA = "Contract Test contract@example.test"
KEYS = {
    "FRED_API_KEY": "FREDCONTRACTKEY0123456789",
    "TIINGO_API_KEY": "TIINGOCONTRACTKEY0123456789",
    "FINNHUB_API_KEY": "FINNHUBCONTRACTKEY0123456789",
}

# One call per tool, all served from fixtures. Add an entry when you register a tool.
CASES: dict[str, dict[str, Any]] = {
    "ping": {},
    "resolve_ticker": {"query": "MSFT"},
    "list_filings": {"ticker": "MSFT", "forms": ["10-K", "10-Q"], "limit": 5},
    "get_filing_section": {
        "ticker": "MSFT",
        "accession": TENK["accession"],
        "section": "1A",
    },
    "get_filing_exhibit": {
        "ticker": "MSFT",
        "accession": EIGHTK["accession"],
        "exhibit": "99.1",
    },
    "get_insider_transactions": {"ticker": "MSFT", "since": "2026-09-16", "limit": 20},
    "get_fundamentals": {"ticker": "MSFT", "n": 3},
    "get_key_ratios": {"ticker": "MSFT", "n": 2},
    "get_price_history": {"ticker": "MSFT", "start": "2026-01-01", "limit": 20},
    "get_price_stats": {"ticker": "MSFT"},
    "get_earnings_calendar": {"tickers": ["MSFT"], "days_ahead": 30},
    "get_macro_series": {"series_ids": ["DGS10", "CPIAUCSL"], "limit": 10},
}

PROMPT_CASES: dict[str, dict[str, str]] = {
    "analyze_ticker": {"ticker": "MSFT"},
}

# Tools whose result carries no figures and no provenance (health check only).
UNSOURCED_TOOLS = {"ping"}

SOURCE_FIELDS = ("source", "source_url", "retrieved_at", "as_of")
LEAKY_URL = re.compile(r"api_key|apikey|token=|user-agent|user_agent|@", re.IGNORECASE)


def _registered_tool_names() -> list[str]:
    return sorted(t.name for t in build_server()._tool_manager.list_tools())


def _registered_prompt_names() -> list[str]:
    return sorted(p.name for p in build_server()._prompt_manager.list_prompts())


def _gz(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


def _json_fixture(folder: str, stem: str) -> bytes:
    base = FIXTURES / folder
    plain = base / f"{stem}.json"
    return plain.read_bytes() if plain.exists() else _gz(base / f"{stem}.json.gz")


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    cache_mod.set_default_cache(Cache(tmp_path / "cache.db"))

    async def fake_sleep(seconds):
        return None

    monkeypatch.setenv("MRM_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(http, "_sleep", fake_sleep)
    monkeypatch.setattr(calendar, "_today", lambda: TODAY)
    monkeypatch.setattr(prices, "_today", lambda: TODAY)
    monkeypatch.setattr(prices, "get_settings", lambda: Settings(price_provider="tiingo"))
    reset_limiters()
    yield
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


@pytest.fixture
def fixtures_only():
    """Serve every outbound request from recorded fixtures; any unmocked request fails."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as m:
        m.get(edgar.TICKERS_URL).mock(
            return_value=httpx.Response(200, content=_gz(SEC / "company_tickers_exchange.json.gz"))
        )
        for path in SEC.glob("CIK*.json.gz"):
            m.get(SUB + path.name.removesuffix(".gz")).mock(
                return_value=httpx.Response(200, content=_gz(path))
            )
        for path in (SEC / "companyfacts").glob("CIK*.json.gz"):
            cik = path.name.removeprefix("CIK").removesuffix(".json.gz")
            m.get(xbrl.companyfacts_url(cik)).mock(
                return_value=httpx.Response(200, content=_gz(path))
            )
        m.get(host="www.sec.gov", path="/cgi-bin/browse-edgar").mock(
            return_value=httpx.Response(200, content=b"<html>no feed</html>")
        )
        # 10-K / 8-K documents.
        for doc, name in (
            (TENK, "msft_10k"),
            (EIGHTK, "msft_8k"),
        ):
            folder = ARCHIVE + doc["accession"].replace("-", "") + "/"
            m.get(folder + doc["accession"] + "-index.htm").mock(
                return_value=httpx.Response(200, content=_gz(DOCS / f"{name}_index.htm.gz"))
            )
        tenk_folder = ARCHIVE + TENK["accession"].replace("-", "") + "/"
        m.get(tenk_folder + TENK["primary_document"]).mock(
            return_value=httpx.Response(200, content=_gz(DOCS / "msft_10k_primary.htm.gz"))
        )
        eightk_folder = ARCHIVE + EIGHTK["accession"].replace("-", "") + "/"
        m.get(eightk_folder + EIGHTK["ex99_1"]).mock(
            return_value=httpx.Response(200, content=_gz(DOCS / "msft_8k_ex99_1.htm.gz"))
        )
        # Form 4 filings.
        for path in FORM4.glob("*.gz"):
            match = re.match(r"(\d{10}-\d{2}-\d{6})-(.+)\.gz$", path.name)
            assert match, path.name
            accession, doc_name = match.groups()
            m.get(f"{ARCHIVE}{accession.replace('-', '')}/{doc_name}").mock(
                return_value=httpx.Response(200, content=_gz(path))
            )
        m.get(url__startswith=ARCHIVE).mock(return_value=httpx.Response(404))
        # Prices (synthetic Tiingo fixture).
        m.get(url__regex=r"https://api\.tiingo\.com/tiingo/daily/msft/prices.*").mock(
            return_value=httpx.Response(200, content=_gz(FIXTURES / "tiingo/msft_prices.json.gz"))
        )
        # FRED.
        for sid in ("DGS10", "CPIAUCSL"):
            m.get(FRED_SERIES_URL, params={"series_id": sid}).mock(
                return_value=httpx.Response(200, content=_json_fixture("fred", f"{sid}_series"))
            )
            m.get(FRED_OBS_URL, params={"series_id": sid}).mock(
                return_value=httpx.Response(
                    200, content=_json_fixture("fred", f"{sid}_observations")
                )
            )
        # Finnhub.
        m.get(url__startswith="https://finnhub.io/api/v1/calendar/earnings").mock(
            return_value=httpx.Response(200, content=_json_fixture("finnhub", "msft_earnings"))
        )
        yield m


# ---------------------------------------------------------------- provenance helpers


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


REASON_FIELDS = ("reason", "note", "notes")


def provenance_violations(node: Any, path: str = "$", owner: dict | None = None) -> list[str]:
    """Every number must sit inside an item whose provenance is complete.

    An item declares provenance when it has any of SOURCE_FIELDS. Nested items inherit the
    fields they do not set (or set to null) from the enclosing item, so a top-level `Sourced`
    result covers its rows. Each field must resolve to a non-empty value; `as_of` may be null
    only with a documented reason (`reason`, `note` or `notes`) on the item or its owner.
    """
    problems: list[str] = []
    if isinstance(node, dict):
        if any(f in node for f in SOURCE_FIELDS):
            inherited = owner or {}
            owner = {
                f: node[f] if node.get(f) not in (None, "") else inherited.get(f)
                for f in (*SOURCE_FIELDS, *REASON_FIELDS)
            }
            documented = any(owner.get(f) for f in REASON_FIELDS)
            for field in ("source", "source_url", "retrieved_at"):
                if not owner.get(field) and not documented:
                    problems.append(f"{path}.{field}: missing or empty without a reason")
            if owner.get("as_of") is None and not documented:
                problems.append(f"{path}.as_of: null without a reason")
        for key, value in node.items():
            problems += provenance_violations(value, f"{path}.{key}", owner)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            problems += provenance_violations(value, f"{path}[{i}]", owner)
    elif _is_number(node) and owner is None:
        problems.append(f"{path}: number {node!r} without source fields")
    return problems


def source_urls(node: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("url") and isinstance(value, str):
                urls.append(value)
            urls += source_urls(value)
    elif isinstance(node, list):
        for value in node:
            urls += source_urls(value)
    return urls


def test_provenance_helper_flags_unsourced_numbers():
    assert provenance_violations({"x": 1})
    assert provenance_violations(
        {"source": "S", "source_url": "u", "retrieved_at": "t", "as_of": None}
    )
    assert not provenance_violations(
        {"source": "S", "source_url": "u", "retrieved_at": "t", "as_of": "d", "v": [1, 2.5]}
    )
    assert not provenance_violations(
        {"source": "S", "source_url": "u", "retrieved_at": "t", "as_of": None, "notes": ["why"]}
    )
    # Rows inherit from the enclosing result; a row may override its own URL.
    top = {"source": "S", "source_url": "u", "retrieved_at": "t", "as_of": "d"}
    assert not provenance_violations({**top, "rows": [{"source_url": "u2", "value": 3}]})
    assert provenance_violations({"rows": [{"source_url": "u2", "as_of": None, "v": 3}]})


def test_every_tool_has_a_contract_case():
    assert set(_registered_tool_names()) == set(CASES)


def test_every_prompt_has_a_contract_case():
    assert set(_registered_prompt_names()) == set(PROMPT_CASES)


# ---------------------------------------------------------------- tools


@pytest.mark.parametrize("name", _registered_tool_names())
async def test_tool_contract(name, fixtures_only):
    assert name in CASES, f"tool {name!r} has no contract case in tests/test_contract.py"
    mcp = build_server()
    tool = {t.name: t for t in await mcp.list_tools()}[name]

    assert tool.description and tool.description.strip(), "tool needs a description"
    assert tool.inputSchema.get("type") == "object"
    jsonschema.Draft202012Validator.check_schema(tool.inputSchema)
    jsonschema.validate(CASES[name], tool.inputSchema)
    assert tool.outputSchema, "tool needs a declared output schema"
    jsonschema.Draft202012Validator.check_schema(tool.outputSchema)

    _, structured = await mcp.call_tool(name, CASES[name])
    jsonschema.validate(structured, tool.outputSchema)

    if name not in UNSOURCED_TOOLS:
        problems = provenance_violations(structured)
        assert not problems, "\n".join(problems[:20])
        assert "source" in json.dumps(structured)

    for url in source_urls(structured):
        assert not LEAKY_URL.search(url), f"source_url leaks a secret or User-Agent: {url}"
    dumped = json.dumps(structured)
    for secret in (*KEYS.values(), UA):
        assert secret not in dumped


# ---------------------------------------------------------------- prompts


@pytest.mark.parametrize("name", _registered_prompt_names())
async def test_prompt_contract(name):
    assert name in PROMPT_CASES, f"prompt {name!r} has no contract case"
    mcp = build_server()
    prompt = {p.name: p for p in await mcp.list_prompts()}[name]
    assert prompt.description and prompt.description.strip()
    required = {a.name for a in prompt.arguments or [] if a.required}
    assert required <= set(PROMPT_CASES[name])
    result = await mcp.get_prompt(name, PROMPT_CASES[name])
    assert result.messages
    text = result.messages[0].content.text
    assert text.strip()
