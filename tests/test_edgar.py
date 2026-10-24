import gzip
import os
from datetime import date

import httpx
import pytest
import respx
from conftest import SEC_FIXTURES, load_sec_fixture

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.errors import BadInput, NotFound
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import edgar

FIXTURES = SEC_FIXTURES
SUB = "https://data.sec.gov/submissions/"
PLACEHOLDER_UA = "Test Agent test@example.test"


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
                return_value=httpx.Response(
                    200, json=load_sec_fixture("company_tickers_exchange.json")
                )
            )
        }
        for path in FIXTURES.glob("CIK*.json.gz"):
            name = path.name.removesuffix(".gz")
            routes[name.removesuffix(".json")] = mock.get(SUB + name).mock(
                return_value=httpx.Response(200, json=load_sec_fixture(path.name))
            )
        routes["_router"] = mock
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
    # The real recent block holds far more than the default limit of 20 10-K/10-Q filings.
    assert len(r.items) == 20 and r.truncated
    assert r.source == "SEC EDGAR" and r.source_url == SUB + "CIK0000789019.json"
    assert r.as_of == dates[0]
    # Expected newest 10-K/10-Q filing date, derived from the recorded fixture.
    recent = load_sec_fixture("CIK0000789019.json")["filings"]["recent"]
    expected = max(
        d
        for d, f in zip(recent["filingDate"], recent["form"], strict=True)
        if f in ("10-K", "10-Q")
    )
    assert r.as_of == date.fromisoformat(expected)
    # Real MSFT FY2025 10-K (fiscal year ended 2025-06-30).
    fy2025 = next(f for f in r.items if f.accession == "0000950170-25-100235")
    assert fy2025.form == "10-K" and fy2025.filing_date == date(2025, 7, 30)
    assert fy2025.report_date == date(2025, 6, 30)
    assert fy2025.url == (
        "https://www.sec.gov/Archives/edgar/data/789019/000095017025100235/msft-20250630.htm"
    )
    edgar.FilingList.model_validate(r.model_dump(mode="json"))
    assert not sec["CIK0000789019-submissions-001"].called


async def test_list_filings_8k_items_and_url(sec):
    r = await edgar.list_filings("MSFT", forms=["8-K"], since="2025-01-01", limit=100)
    assert r.items and all(f.form == "8-K" for f in r.items)
    # Every filing URL follows the EDGAR Archives pattern.
    for f in r.items:
        acc = f.accession.replace("-", "")
        assert f.url == (
            f"https://www.sec.gov/Archives/edgar/data/789019/{acc}/{f.primary_document}"
        )
    # Earnings releases are filed as 8-K item 2.02.
    earnings = [f for f in r.items if f.items and "2.02" in f.items.split(",")]
    assert len(earnings) >= 4
    # Real MSFT FY2025 Q4 earnings 8-K.
    q4 = next(f for f in r.items if f.accession == "0000950170-25-100226")
    assert q4.items == "2.02,9.01"
    assert q4.filing_date == date(2025, 7, 30) and q4.report_date == date(2025, 7, 30)
    assert q4.url == (
        "https://www.sec.gov/Archives/edgar/data/789019/000095017025100226/msft-20250730.htm"
    )


async def test_list_filings_limit_truncated(sec):
    r = await edgar.list_filings("MSFT", limit=3)
    assert len(r.items) == 3 and r.truncated and r.limit == 3


async def test_list_filings_since_within_recent(sec):
    r = await edgar.list_filings("MSFT", since="2025-07-30")
    assert r.items and all(f.filing_date >= date(2025, 7, 30) for f in r.items)
    assert not sec["CIK0000789019-submissions-001"].called


async def test_list_filings_since_fetches_older_page(sec):
    # The recorded recent block starts in 2020-09, so 2019 needs the older page 001.
    r = await edgar.list_filings("MSFT", forms=["10-K"], since="2019-01-01")
    dates = [f.filing_date for f in r.items]
    assert dates == sorted(dates, reverse=True)
    assert date(2025, 7, 30) in dates and date(2024, 7, 30) in dates
    assert dates[-2:] == [date(2020, 7, 30), date(2019, 8, 1)]
    old = r.items[-1]
    assert old.accession == "0001564590-19-027952"
    assert old.primary_document == "msft-10k_20190630.htm"
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
    paths = sorted(FIXTURES.glob("*.json.gz"))
    assert paths
    for path in paths:
        text = gzip.decompress(path.read_bytes()).decode("utf-8")
        assert "@" not in text, path
        assert "user-agent" not in text.lower(), path


def test_fixtures_are_compressed():
    assert not list(FIXTURES.glob("*.json")), "store SEC fixtures as .json.gz"


# ---------------------------------------------------------------- live


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("SEC_USER_AGENT"), reason="SEC_USER_AGENT not set")
async def test_live_resolve_and_list():
    r = await edgar.resolve_ticker("MSFT")
    assert r.cik == "0000789019" and r.sic == "7372"
    filings = await edgar.list_filings("MSFT", forms=["10-K"], limit=2)
    assert filings.items and filings.items[0].form == "10-K"


# ---------------------------------------------------------------- F2 review fixes


async def test_list_filings_forms_pages_older_when_recent_short(sec):
    # MSFT's recent block holds 6 10-Ks; asking for 10 must continue into page 001.
    r = await edgar.list_filings("MSFT", forms=["10-K"], limit=10)
    assert len(r.items) == 10 and all(f.form == "10-K" for f in r.items)
    dates = [f.filing_date for f in r.items]
    assert dates == sorted(dates, reverse=True)
    assert date(2019, 8, 1) in dates
    assert sec["CIK0000789019-submissions-001"].call_count == 1
    assert not any("older" in n for n in r.notes)


def _empty_block() -> dict:
    keys = ("accessionNumber", "filingDate", "reportDate", "form", "primaryDocument", "items")
    return {k: [] for k in keys}


def _jpm_page(n: int) -> str:
    return f"{SUB}CIK0000019617-submissions-{n:03d}.json"


async def test_list_filings_heavy_filer_jpm(sec):
    # Real JPM data: the recent block (one year of mostly 424B2s) has a single 10-K, and real
    # page 001 has none. Page 002 is synthetic: two earlier annual 10-Ks.
    block = _empty_block()
    for acc, filed in (
        ("0000019617-25-000270", "2025-02-14"),
        ("0000019617-24-000225", "2024-02-16"),
    ):
        block["accessionNumber"].append(acc)
        block["filingDate"].append(filed)
        block["reportDate"].append(f"{int(filed[:4]) - 1}-12-31")
        block["form"].append("10-K")
        block["primaryDocument"].append("jpm-10k.htm")
        block["items"].append("")
    p2 = sec["_router"].get(_jpm_page(2)).mock(return_value=httpx.Response(200, json=block))
    p3 = (
        sec["_router"].get(_jpm_page(3)).mock(return_value=httpx.Response(200, json=_empty_block()))
    )
    r = await edgar.list_filings("JPM", forms=["10-K"], limit=3)
    assert len(r.items) >= 3 and all(f.form == "10-K" for f in r.items)
    assert [f.filing_date.year for f in r.items] == [2026, 2025, 2024]
    assert sec["CIK0000019617-submissions-001"].call_count == 1
    assert p2.call_count == 1 and not p3.called


async def test_list_filings_older_pages_capped_with_note(sec):
    # Real recent + real page 001 (no 10-K), then empty pages: stop after the cap and say so.
    routes = [
        sec["_router"].get(_jpm_page(n)).mock(return_value=httpx.Response(200, json=_empty_block()))
        for n in range(2, 10)
    ]
    r = await edgar.list_filings("JPM", forms=["10-K"], limit=3)
    assert len(r.items) == 1 and r.truncated
    fetched = 1 + sum(route.called for route in routes)  # page 001 + synthetic pages
    assert fetched == edgar.MAX_EXTRA_PAGES
    assert any("older" in n and str(edgar.MAX_EXTRA_PAGES) in n for n in r.notes)


@pytest.mark.parametrize(
    ("typo", "suggestion"), [("MSF", "MSFT"), ("AMZ", "AMZN"), ("msf", "MSFT")]
)
async def test_ticker_typo_not_found_with_suggestions(sec, typo, suggestion):
    with pytest.raises(NotFound) as ei:
        await edgar.get_company(typo)
    assert suggestion in str(ei.value)
    with pytest.raises(NotFound):
        await edgar.resolve_ticker(typo)


@pytest.mark.parametrize("name", ["Microsoft", "microsoft corp", "MICROSOFT CORP"])
async def test_name_query_still_resolves(sec, name):
    assert (await edgar.get_company(name)).ticker == "MSFT"


@pytest.mark.parametrize(
    ("query", "ticker"),
    [
        ("MSFT", "MSFT"),
        ("msft", "MSFT"),
        ("Apple", "AAPL"),
        ("apple", "AAPL"),
        ("Intel", "INTC"),
        ("Visa", "V"),
        ("Nike", "NKE"),
        ("Tesla", "TSLA"),
        ("NVIDIA", "NVDA"),
    ],
)
async def test_short_company_names_resolve(sec, query, ticker):
    assert (await edgar.get_company(query)).ticker == ticker


async def test_ambiguous_name_not_found_with_suggestions(sec):
    # "Intelligent" starts several different companies' names: no unique best match.
    with pytest.raises(NotFound) as ei:
        await edgar.get_company("Intelligent")
    assert "did you mean" in str(ei.value)


async def test_nonsense_name_not_found(sec):
    with pytest.raises(NotFound):
        await edgar.get_company("qwxzv plorkington")
