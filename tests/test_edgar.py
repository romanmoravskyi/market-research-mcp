import json
import os
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.errors import BadInput, NotFound
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import edgar

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
SUB = "https://data.sec.gov/submissions/"
PLACEHOLDER_UA = "Test Agent test@example.test"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    cache_mod.set_default_cache(Cache(tmp_path / "cache.db"))
    reset_limiters()
    yield
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


@pytest.fixture
def ua(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", PLACEHOLDER_UA)


@pytest.fixture
def sec(ua):
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        routes = {
            "tickers": mock.get(edgar.TICKERS_URL).mock(
                return_value=httpx.Response(200, json=load("company_tickers_exchange.json"))
            )
        }
        for path in FIXTURES.glob("CIK*.json"):
            routes[path.stem] = mock.get(SUB + path.name).mock(
                return_value=httpx.Response(200, json=load(path.name))
            )
        yield routes


# ---------------------------------------------------------------- resolve_ticker


async def test_resolve_by_ticker(sec):
    r = await edgar.resolve_ticker("msft")
    assert r.cik == "0000789019"
    assert r.name == "MICROSOFT CORP" and r.ticker == "MSFT" and r.exchange == "Nasdaq"
    assert r.sic == "7372" and r.sic_description == "Services-Prepackaged Software"
    assert r.source == "SEC EDGAR" and r.source_url == edgar.TICKERS_URL
    assert r.retrieved_at.tzinfo is not None and r.as_of is not None
    edgar.ResolvedTicker.model_validate(r.model_dump(mode="json"))
    assert sec["tickers"].calls.last.request.headers["User-Agent"] == PLACEHOLDER_UA


@pytest.mark.parametrize("query", ["BRK.B", "brk-b", "BRK/B"])
async def test_resolve_dotted_ticker(sec, query):
    r = await edgar.resolve_ticker(query)
    assert r.cik == "0001067983" and r.ticker == "BRK-B"
    assert r.sic == "6331"


async def test_resolve_by_name(sec):
    r = await edgar.resolve_ticker("realty income")
    assert r.ticker == "O" and r.cik == "0000726728"
    assert r.sic_description == "Real Estate Investment Trusts"


async def test_resolve_by_cik_digits(sec):
    assert (await edgar.resolve_ticker("19617")).ticker == "JPM"


async def test_resolve_not_found(sec):
    with pytest.raises(NotFound):
        await edgar.resolve_ticker("ZZZZNOTREAL")


async def test_resolve_empty_is_bad_input(sec):
    with pytest.raises(BadInput):
        await edgar.resolve_ticker("  ")


async def test_get_cik_helper(sec):
    assert await edgar.get_cik("JPM") == "0000019617"


# ---------------------------------------------------------------- list_filings


async def test_list_filings_form_filter(sec):
    r = await edgar.list_filings("MSFT", forms=["10-k", "10-Q"])
    assert {f.form for f in r.items} == {"10-K", "10-Q"}
    dates = [f.filing_date for f in r.items]
    assert dates == sorted(dates, reverse=True)
    assert not r.truncated
    assert r.source == "SEC EDGAR" and r.source_url == SUB + "CIK0000789019.json"
    assert r.as_of == date(2025, 10, 29)
    edgar.FilingList.model_validate(r.model_dump(mode="json"))
    assert not sec["CIK0000789019-submissions-001"].called


async def test_list_filings_8k_items_and_url(sec):
    r = await edgar.list_filings("MSFT", forms=["8-K"], limit=1)
    f = r.items[0]
    assert f.items == "2.02,9.01"
    assert f.accession == "0000950170-25-129900"
    assert f.url == (
        "https://www.sec.gov/Archives/edgar/data/789019/000095017025129900/msft-20251029.htm"
    )
    assert f.report_date == date(2025, 10, 29)


async def test_list_filings_limit_truncated(sec):
    r = await edgar.list_filings("MSFT", limit=3)
    assert len(r.items) == 3 and r.truncated and r.limit == 3


async def test_list_filings_since_within_recent(sec):
    r = await edgar.list_filings("MSFT", since="2025-07-30")
    assert r.items and all(f.filing_date >= date(2025, 7, 30) for f in r.items)
    assert not sec["CIK0000789019-submissions-001"].called


async def test_list_filings_since_fetches_older_page(sec):
    r = await edgar.list_filings("MSFT", forms=["10-K"], since="2024-01-01")
    assert [f.filing_date for f in r.items] == [date(2025, 7, 30), date(2024, 7, 30)]
    assert sec["CIK0000789019-submissions-001"].call_count == 1


@pytest.mark.parametrize("bad", ["2025-13-01", "01/02/2025", "2025-1-1", "yesterday"])
async def test_list_filings_bad_date(sec, bad):
    with pytest.raises(BadInput, match="YYYY-MM-DD"):
        await edgar.list_filings("MSFT", since=bad)


async def test_list_filings_bad_limit(sec):
    with pytest.raises(BadInput):
        await edgar.list_filings("MSFT", limit=0)


async def test_list_filings_data_helper(sec):
    items, truncated, sub = await edgar.list_filings_data(19617, forms=["10-K"])
    assert len(items) == 1 and not truncated
    assert items[0].url.startswith("https://www.sec.gov/Archives/edgar/data/19617/")
    assert sub.data["name"] == "JPMORGAN CHASE & CO"


# ---------------------------------------------------------------- cache, registration


async def test_cache_reuse_no_second_http(sec):
    await edgar.resolve_ticker("MSFT")
    await edgar.list_filings("MSFT")
    second = await edgar.list_filings("MSFT")
    assert sec["tickers"].call_count == 1
    assert sec["CIK0000789019"].call_count == 1
    assert second.items


async def test_tools_registered():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("t")
    edgar.register(mcp)
    names = {t.name for t in await mcp.list_tools()}
    assert {"resolve_ticker", "list_filings"} <= names


def test_fixtures_have_no_user_agent():
    for path in FIXTURES.rglob("*.json"):
        assert "@" not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------- live


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("SEC_USER_AGENT"), reason="SEC_USER_AGENT not set")
async def test_live_resolve_and_list():
    r = await edgar.resolve_ticker("MSFT")
    assert r.cik == "0000789019" and r.sic == "7372"
    filings = await edgar.list_filings("MSFT", forms=["10-K"], limit=2)
    assert filings.items and filings.items[0].form == "10-K"
