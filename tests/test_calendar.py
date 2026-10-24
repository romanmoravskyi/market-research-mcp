import gzip
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from mcp.server.fastmcp import FastMCP

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.config import load_settings
from market_research_mcp.errors import BadInput
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import calendar, edgar

FIXTURES = Path(__file__).parent / "fixtures"
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
SUB = "https://data.sec.gov/submissions/"
KEY = "FINNHUBTESTKEY0123456789"
TODAY = date(2026, 10, 24)
MSFT_8K_2025Q4 = edgar.accession_url(789019, "0001193125-25-256310", "msft-20251028.htm")


def load_json(folder: str, stem: str) -> dict:
    """Load `stem.json` or `stem.json.gz` (fixtures may be gzipped)."""
    base = FIXTURES / folder
    plain = base / f"{stem}.json"
    if plain.exists():
        return json.loads(plain.read_text(encoding="utf-8"))
    return json.loads(gzip.decompress((base / f"{stem}.json.gz").read_bytes()))


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    cache_mod.set_default_cache(Cache(tmp_path / "cache.db"))

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(http, "_sleep", fake_sleep)
    monkeypatch.setattr(calendar, "_today", lambda: TODAY)
    monkeypatch.setenv("FINNHUB_API_KEY", KEY)
    monkeypatch.setenv("SEC_USER_AGENT", "Test Agent test@example.test")
    reset_limiters()
    yield
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as m:
        m.get(edgar.TICKERS_URL).mock(
            return_value=httpx.Response(200, json=load_json("sec", "company_tickers_exchange"))
        )
        m.get(SUB + "CIK0000789019.json").mock(
            return_value=httpx.Response(200, json=load_json("sec", "CIK0000789019"))
        )
        yield m


def finnhub_route(m, status=200, body=None):
    if body is None:
        body = load_json("finnhub", "msft_earnings")
    return m.get(FINNHUB_URL).mock(return_value=httpx.Response(status, json=body))


def assert_no_key(res, route=None):
    assert KEY not in res.model_dump_json()
    if route is not None:
        for call in route.calls:
            assert KEY not in str(call.request.url)
            assert call.request.headers["X-Finnhub-Token"] == KEY


# ---------------------------------------------------------------- Finnhub primary


async def test_finnhub_hit(mock):
    route = finnhub_route(mock)
    res = await calendar.get_earnings_calendar(["msft"], days_ahead=30)
    assert len(res.rows) == 1
    row = res.rows[0]
    assert row.ticker == "MSFT" and row.method == "finnhub"
    assert row.next_earnings_date == date(2026, 11, 3)
    assert row.hour == "amc"
    assert row.fiscal_quarter == 1 and row.fiscal_year == 2027
    assert row.confirmed is None and row.estimated is False
    assert row.source == "Finnhub" and row.source_url == calendar.FINNHUB_DOCS_URL
    assert any("confirm" in n.lower() for n in row.notes)
    dumped = row.model_dump()
    forecasts = {"epsEstimate", "revenueEstimate", "eps_estimate", "revenue_estimate"}
    assert not forecasts & set(dumped)
    params = route.calls.last.request.url.params
    assert params["symbol"] == "MSFT"
    assert params["from"] == "2026-10-24" and params["to"] == "2026-11-23"
    assert res.window_start == TODAY and res.window_end == date(2026, 11, 23)
    assert res.source == "Finnhub"
    assert_no_key(res, route)


async def test_finnhub_far_date_uses_wider_window(mock):
    finnhub_route(mock)
    res = await calendar.get_earnings_calendar(["MSFT"], days_ahead=5)
    # Finnhub row 2026-11-03 is outside a 5-day window -> EDGAR estimate (2026-10-29).
    row = res.rows[0]
    assert row.method == "edgar_estimate"
    assert row.next_earnings_date == date(2026, 10, 29)


# ---------------------------------------------------------------- EDGAR fallback


async def test_empty_finnhub_falls_back_to_edgar_estimate(mock):
    route = finnhub_route(mock, body=load_json("finnhub", "empty"))
    res = await calendar.get_earnings_calendar(["MSFT"], days_ahead=30)
    row = res.rows[0]
    assert row.method == "edgar_estimate" and row.estimated is True
    assert row.next_earnings_date == date(2026, 10, 29)  # 2025-10-29 8-K + 1 year
    assert row.estimate_basis == "prior_year_same_quarter"
    assert row.source == calendar.EDGAR_SOURCE
    assert row.source_url == MSFT_8K_2025Q4
    assert row.as_of == date(2025, 10, 29)
    assert row.hour is None and row.confirmed is False
    assert any("Finnhub" in n for n in row.notes)
    assert res.source == calendar.EDGAR_SOURCE
    assert_no_key(res, route)


async def test_403_falls_back(mock):
    route = finnhub_route(mock, status=403, body={"error": "You don't have access."})
    res = await calendar.get_earnings_calendar(["MSFT"])
    row = res.rows[0]
    assert row.method == "edgar_estimate" and row.estimated
    assert any("403" in n for n in row.notes)
    assert_no_key(res, route)


async def test_missing_key_falls_back_with_note(mock, monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY")
    route = finnhub_route(mock)
    res = await calendar.get_earnings_calendar(["MSFT"])
    assert not route.called
    row = res.rows[0]
    assert row.method == "edgar_estimate"
    assert any("FINNHUB_API_KEY" in n for n in row.notes)


async def test_unknown_ticker_row_has_method_none(mock):
    finnhub_route(mock, body=load_json("finnhub", "empty"))
    res = await calendar.get_earnings_calendar(["MSFT", "ZZZZNOTREAL"])
    by = {r.ticker: r for r in res.rows}
    assert by["MSFT"].method == "edgar_estimate"
    none = by["ZZZZNOTREAL"]
    assert none.method == "none" and none.next_earnings_date is None
    assert none.source and none.source_url
    assert res.source == calendar.EDGAR_SOURCE


async def test_mixed_methods_each_row_states_source(mock):
    finnhub_route(mock)  # only MSFT rows; AAPL gets nothing from Finnhub
    mock.get(SUB + "CIK0000320193.json").mock(return_value=httpx.Response(404))
    res = await calendar.get_earnings_calendar(["MSFT", "AAPL"])
    by = {r.ticker: r for r in res.rows}
    assert by["MSFT"].method == "finnhub" and by["MSFT"].source == "Finnhub"
    assert by["AAPL"].method == "none" and by["AAPL"].source == calendar.EDGAR_SOURCE
    assert res.source.startswith("Mixed")


def test_estimate_cadence_when_year_ago_outside_window():
    filings = [date(2026, 7, 29), date(2026, 4, 29)]
    est = calendar.estimate_next(filings, TODAY, date(2026, 11, 23))
    assert est is not None
    assert est.basis == "quarterly_cadence"
    assert est.date == date(2026, 10, 28)  # 2026-07-29 + 91 days
    assert est.basis_filing == date(2026, 7, 29)


def test_estimate_none_when_nothing_in_window():
    assert calendar.estimate_next([date(2026, 9, 1)], TODAY, date(2026, 10, 30)) is None
    assert calendar.estimate_next([], TODAY, date(2026, 10, 30)) is None


# ---------------------------------------------------------------- input checks


async def test_ticker_limit():
    with pytest.raises(BadInput):
        await calendar.get_earnings_calendar([f"T{i}" for i in range(21)])
    with pytest.raises(BadInput):
        await calendar.get_earnings_calendar([])


@pytest.mark.parametrize("days", [0, 181])
async def test_days_ahead_bounds(days):
    with pytest.raises(BadInput):
        await calendar.get_earnings_calendar(["MSFT"], days_ahead=days)


async def test_register():
    mcp = FastMCP("t")
    calendar.register(mcp)
    names = {t.name for t in await mcp.list_tools()}
    assert "get_earnings_calendar" in names


# ---------------------------------------------------------------- live


@pytest.mark.live
async def test_live_finnhub_msft(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)  # drop the fake keys; read .env
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(calendar, "_today", date.today)
    if not load_settings().finnhub_api_key:
        pytest.skip("FINNHUB_API_KEY not set")
    res = await calendar.get_earnings_calendar(["MSFT"], days_ahead=120)
    row = res.rows[0]
    assert row.method in {"finnhub", "edgar_estimate"}
    assert row.source and row.source_url
