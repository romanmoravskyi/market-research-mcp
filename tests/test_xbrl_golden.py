"""Golden test: MSFT annual figures from companyfacts must equal the printed 10-K values.

Expected values were taken independently of the companyfacts API: each 10-K's own
"CONSOLIDATED STATEMENT OF INCOME" (In millions, except per share data), rendered from the
filing via edgartools `Company("MSFT").get_filings(form="10-K")[i].obj().income_statement`
on 2026-10-24. Millions are converted to full USD; EPS is in dollars per share.
Microsoft's fiscal year ends June 30.
"""

import gzip
import json
from pathlib import Path

import httpx
import pytest
import respx

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import edgar, xbrl

SEC = Path(__file__).parent / "fixtures" / "sec"

# FY2026: 10-K accession 0001193125-26-323660, filed 2026-07-29
#   Revenue $331,839 ; Net income $133,749 ; Diluted EPS 17.95
# FY2025: 10-K accession 0000950170-25-100235, filed 2025-07-30
#   Revenue $281,724 ; Net income $101,832 ; Diluted EPS 13.64
# FY2024: 10-K accession 0000950170-24-087843, filed 2024-07-30
#   Revenue $245,122 ; Net income $88,136 ; Diluted EPS 11.80
# FY2023: 10-K accession 0000950170-23-035122, filed 2023-07-27
#   Revenue $211,915 ; Net income $72,361 ; Diluted EPS 9.68
GOLDEN = {
    2026: {"revenue": 331_839_000_000, "net_income": 133_749_000_000, "diluted_eps": 17.95},
    2025: {"revenue": 281_724_000_000, "net_income": 101_832_000_000, "diluted_eps": 13.64},
    2024: {"revenue": 245_122_000_000, "net_income": 88_136_000_000, "diluted_eps": 11.80},
    2023: {"revenue": 211_915_000_000, "net_income": 72_361_000_000, "diluted_eps": 9.68},
}


def load_tickers() -> dict:
    gz = SEC / "company_tickers_exchange.json.gz"
    if gz.exists():
        return json.loads(gzip.decompress(gz.read_bytes()))
    return json.loads((SEC / "company_tickers_exchange.json").read_text("utf-8"))


@pytest.fixture(autouse=True)
async def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Agent test@example.test")
    cache_mod.set_default_cache(Cache(tmp_path / "cache.db"))
    reset_limiters()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        tickers = load_tickers()
        mock.get(edgar.TICKERS_URL).mock(return_value=httpx.Response(200, json=tickers))
        raw = gzip.decompress((SEC / "companyfacts" / "CIK0000789019.json.gz").read_bytes())
        mock.get(xbrl.companyfacts_url("789019")).mock(
            return_value=httpx.Response(200, content=raw)
        )
        yield
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


async def test_msft_annual_golden():
    res = await xbrl.get_fundamentals(
        "MSFT", metrics=["revenue", "net_income", "diluted_eps"], periods="annual", n=4
    )
    got = {(v.fiscal_year, v.metric): v.value for v in res.values}
    for fy, expected in GOLDEN.items():
        for metric, value in expected.items():
            assert got[(fy, metric)] == value, (fy, metric)
    years = sorted({v.fiscal_year for v in res.values}, reverse=True)
    assert years == [2026, 2025, 2024, 2023]
    for v in res.values:
        assert v.period_end.month == 6 and v.period_end.day == 30
        assert v.form.startswith("10-K") and not v.derived
