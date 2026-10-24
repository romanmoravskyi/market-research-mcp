"""Golden tests: figures from the recorded companyfacts must equal the filed 10-K/10-Q values.

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
CIKS = {
    "MSFT": "789019",
    "AAPL": "320193",
    "BRK-B": "1067983",
    "JPM": "19617",
    "O": "726728",
    "NVDA": "1045810",
}

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
        for cik in CIKS.values():
            path = SEC / "companyfacts" / f"CIK{edgar.pad_cik(cik)}.json.gz"
            mock.get(xbrl.companyfacts_url(cik)).mock(
                return_value=httpx.Response(200, content=gzip.decompress(path.read_bytes()))
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


# ---------------------------------------------------------------- other filers (T9)
# Values below are the figures in each company's 10-K as tagged in the recorded companyfacts;
# the accession is the 10-K that first reported the year (later 10-Ks repeat it unchanged).
# Full USD (10-K tables are in millions, Realty Income's in thousands); EPS in USD per share.

ANNUAL_GOLDEN = {
    # Apple, FY ends late September. FY2025: 10-K 0000320193-25-000079 (filed 2025-10-31);
    # FY2024: 10-K 0000320193-24-000123 (filed 2024-11-01).
    "AAPL": {
        2025: {
            "revenue": 416_161_000_000,
            "gross_profit": 195_201_000_000,
            "operating_income": 133_050_000_000,
            "net_income": 112_010_000_000,
            "diluted_eps": 7.46,
        },
        2024: {
            "revenue": 391_035_000_000,
            "gross_profit": 180_683_000_000,
            "operating_income": 123_216_000_000,
            "net_income": 93_736_000_000,
            "diluted_eps": 6.08,
        },
    },
    # Berkshire Hathaway (insurer, SIC 6331): total revenues and net earnings attributable to
    # shareholders. FY2025: 10-K 0001193125-26-083899; FY2024: 10-K 0000950170-25-025210.
    "BRK-B": {
        2025: {"revenue": 371_444_000_000, "net_income": 66_968_000_000},
        2024: {"revenue": 371_433_000_000, "net_income": 88_995_000_000},
    },
    # JPMorgan Chase (bank): total net revenue (Revenues / RevenuesNetOfInterestExpense).
    # FY2025: 10-K 0001628280-26-008131; FY2024: 10-K 0000019617-25-000270.
    "JPM": {
        2025: {"revenue": 182_447_000_000, "net_income": 57_048_000_000, "diluted_eps": 20.02},
        2024: {"revenue": 177_556_000_000, "net_income": 58_471_000_000, "diluted_eps": 19.75},
    },
    # Realty Income (REIT). FY2025: 10-K 0000726728-26-000011; FY2024: 10-K
    # 0000726728-25-000055.
    "O": {
        2025: {"revenue": 5_749_377_000, "net_income": 1_058_590_000, "diluted_eps": 1.17},
        2024: {"revenue": 5_271_142_000, "net_income": 860_772_000, "diluted_eps": 0.98},
    },
    # NVIDIA, FY ends late January. FY2025 (ended 2025-01-26): 10-K 0001045810-25-000023.
    # FY2024 EPS and shares are NVIDIA's own figures restated for the June 2024 10:1 split in
    # that 10-K (the FY2024 10-K 0001045810-24-000029 printed $11.93 and 2,494M shares).
    "NVDA": {
        2025: {
            "revenue": 130_497_000_000,
            "net_income": 72_880_000_000,
            "diluted_eps": 2.94,
            "shares_diluted": 24_804_000_000,
        },
        2024: {
            "revenue": 60_922_000_000,
            "net_income": 29_760_000_000,
            "diluted_eps": 1.19,
            "shares_diluted": 24_940_000_000,
        },
    },
}


@pytest.mark.parametrize("ticker", list(ANNUAL_GOLDEN))
async def test_annual_golden(ticker):
    golden = ANNUAL_GOLDEN[ticker]
    metrics = sorted({m for year in golden.values() for m in year})
    res = await xbrl.get_fundamentals(ticker, metrics=metrics, periods="annual", n=3)
    got = {(v.fiscal_year, v.metric): v for v in res.values}
    for fy, expected in golden.items():
        for metric, value in expected.items():
            v = got[(fy, metric)]
            assert v.value == value, (ticker, fy, metric)
            assert v.split_factor == 1 and not v.derived


async def test_nvda_pre_split_values_adjusted():
    # FY2022 (ended 2022-01-30) was never restated for the 2024 10:1 split. Its latest SEC
    # figures (FY2024 10-K 0001045810-24-000029, as first printed in FY2022 10-K
    # 0001045810-22-000036, already restated for the 2021 4:1 split): diluted EPS $3.85,
    # diluted shares 2,535M. On today's basis: $0.385 and 25,350M.
    # FY2021 (ended 2021-01-31): FY2022/FY2023 10-Ks restated it for the 2021 split to
    # $1.73 and 2,510M (the FY2021 10-K 0001045810-21-000010 printed $6.90 and 628M);
    # x10 for 2024 gives $0.173 and 25,100M.
    res = await xbrl.get_fundamentals(
        "NVDA", metrics=["diluted_eps", "shares_diluted"], periods="annual", n=6
    )
    got = {(v.fiscal_year, v.metric): v for v in res.values}
    assert got[(2022, "diluted_eps")].value == 0.385
    assert got[(2022, "shares_diluted")].value == 25_350_000_000
    assert got[(2021, "diluted_eps")].value == 0.173
    assert got[(2021, "shares_diluted")].value == 25_100_000_000
    for fy in (2021, 2022):
        v = got[(fy, "shares_diluted")]
        assert v.split_factor == 10 and "10:1" in v.note
    # FY2023 was restated by NVIDIA itself (10-K 0001045810-25-000023): $0.17, 25,070M.
    assert got[(2023, "diluted_eps")].value == 0.17
    assert got[(2023, "diluted_eps")].split_factor == 1


async def test_derived_quarters_golden():
    # MSFT FY2025 Q4 revenue = FY 10-K 0000950170-25-100235 ($281,724M) - 9M YTD 10-Q
    # 0000950170-25-061046 ($205,283M) = $76,441M (Microsoft's reported Q4 FY2025 revenue).
    msft = await xbrl.get_fundamentals("MSFT", metrics=["revenue"], periods="quarterly", n=8)
    q4 = next(v for v in msft.values if (v.fiscal_year, v.fiscal_period) == (2025, "Q4"))
    assert q4.value == 76_441_000_000 and q4.derived

    # AAPL FY2025: revenue Q4 = FY 10-K 0000320193-25-000079 ($416,161M) - 9M YTD 10-Q
    # 0000320193-25-000073 ($313,695M) = $102,466M. Operating cash flow is year-to-date only
    # in 10-Qs (3M $29,935M 10-Q 0000320193-25-000008; 6M $53,887M 10-Q 0000320193-25-000057;
    # 9M $81,754M 10-Q 0000320193-25-000073; FY $111,482M).
    aapl = await xbrl.get_fundamentals(
        "AAPL", metrics=["revenue", "operating_cash_flow"], periods="quarterly", n=8
    )
    got = {(v.fiscal_year, v.fiscal_period, v.metric): v for v in aapl.values}
    assert got[(2025, "Q4", "revenue")].value == 102_466_000_000
    ocf = [got[(2025, q, "operating_cash_flow")] for q in ("Q1", "Q2", "Q3", "Q4")]
    assert [v.value for v in ocf] == [
        29_935_000_000,
        23_952_000_000,
        27_867_000_000,
        29_728_000_000,
    ]
    assert [v.derived for v in ocf] == [False, True, True, True]
