import gzip
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.errors import BadInput
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import edgar, xbrl
from market_research_mcp.sources.xbrl import Fact

SEC = Path(__file__).parent / "fixtures" / "sec"
CIKS = {"MSFT": "789019", "JPM": "19617", "O": "726728", "NVDA": "1045810"}


def load_tickers() -> dict:
    gz = SEC / "company_tickers_exchange.json.gz"
    if gz.exists():
        return json.loads(gzip.decompress(gz.read_bytes()))
    return json.loads((SEC / "company_tickers_exchange.json").read_text("utf-8"))


def fact(
    concept: str,
    start: str | None,
    end: str,
    value: float,
    *,
    filed: str = "2025-02-01",
    form: str = "10-K",
    accession: str = "0000000001-25-000001",
    unit: str = "USD",
) -> Fact:
    return Fact(
        concept=concept,
        unit=unit,
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end),
        value=value,
        accession=accession,
        form=form,
        filed=date.fromisoformat(filed),
    )


# ---------------------------------------------------------------- pure layer


def test_ytd_10q_fact_dropped_by_duration():
    facts = [
        fact("NetIncomeLoss", "2024-04-01", "2024-06-30", 10, form="10-Q"),
        fact("NetIncomeLoss", "2024-01-01", "2024-06-30", 25, form="10-Q"),  # 6-month YTD
        fact("NetIncomeLoss", "2024-01-01", "2024-12-31", 50),
    ]
    q = xbrl.select_quarterly(facts)
    assert [(f.start, f.value) for f in q] == [(date(2024, 4, 1), 10)]
    a = xbrl.select_annual(facts)
    assert [f.value for f in a] == [50]


def test_non_periodic_forms_ignored():
    facts = [fact("NetIncomeLoss", "2024-01-01", "2024-12-31", 50, form="8-K")]
    assert xbrl.select_annual(facts) == []


def test_dedupe_keeps_latest_filed():
    facts = [
        fact("Revenues", "2023-01-01", "2023-12-31", 100, filed="2024-02-01", accession="A-1"),
        fact("Revenues", "2023-01-01", "2023-12-31", 105, filed="2025-02-01", accession="A-2"),
        fact("Revenues", "2023-01-01", "2023-12-31", 99, filed="2024-06-01", form="10-K/A"),
    ]
    out = xbrl.dedupe(facts)
    assert len(out) == 1
    assert out[0].value == 105 and out[0].accession == "A-2" and out[0].filed == date(2025, 2, 1)


def test_tag_fallback_is_per_period():
    facts = [
        fact("Revenues", "2018-01-01", "2018-12-31", 80, accession="0000000001-19-000001"),
        fact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "2019-01-01",
            "2019-12-31",
            90,
            accession="0000000001-20-000001",
        ),
        fact("NetIncomeLoss", "2019-01-01", "2019-12-31", 9),
        fact("NetIncomeLoss", "2018-01-01", "2018-12-31", 8),
    ]
    vals = xbrl.normalize(facts, cik="1", metrics=["revenue"], periods="annual", n=5)
    by_year = {v.fiscal_year: v for v in vals}
    assert by_year[2019].tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert by_year[2019].value == 90
    assert by_year[2018].tag == "Revenues" and by_year[2018].value == 80
    assert by_year[2019].source_url == (
        "https://www.sec.gov/Archives/edgar/data/1/000000000120000001/"
    )


def test_n_limits_periods_newest_first():
    facts = [
        fact("NetIncomeLoss", f"{y}-01-01", f"{y}-12-31", y, accession=f"0000000001-{y}-1")
        for y in range(2015, 2025)
    ]
    vals = xbrl.normalize(facts, cik="1", metrics=["net_income"], periods="annual", n=3)
    assert [v.fiscal_year for v in vals] == [2024, 2023, 2022]


def test_instant_metric_matched_to_period_end_and_total_debt():
    facts = [
        fact("NetIncomeLoss", "2024-01-01", "2024-12-31", 5),
        fact("CashAndCashEquivalentsAtCarryingValue", None, "2024-12-31", 7),
        fact("CashAndCashEquivalentsAtCarryingValue", None, "2024-09-30", 6, form="10-Q"),
        fact("LongTermDebtNoncurrent", None, "2024-12-31", 100),
        fact("LongTermDebtCurrent", None, "2024-12-31", 20),
    ]
    vals = xbrl.normalize(facts, cik="1", metrics=["cash", "total_debt"], periods="annual", n=5)
    got = {v.metric: v for v in vals}
    assert got["cash"].value == 7 and got["cash"].period_start is None
    assert got["total_debt"].value == 120
    assert got["total_debt"].tag == "LongTermDebtNoncurrent+LongTermDebtCurrent"


def test_missing_tag_returns_null_with_reason():
    facts = [fact("NetIncomeLoss", "2024-01-01", "2024-12-31", 5)]
    vals = xbrl.normalize(facts, cik="1", metrics=["gross_profit"], periods="annual", n=5)
    assert len(vals) == 1
    assert vals[0].value is None and vals[0].reason == "no matching us-gaap tag"
    assert vals[0].tag is None


def test_fiscal_year_from_period_end():
    assert xbrl.fiscal_year_for(date(2026, 6, 30)) == 2026
    assert xbrl.fiscal_year_for(date(2025, 12, 31)) == 2025
    # 52/53-week years that end in the first days of January belong to the prior year
    assert xbrl.fiscal_year_for(date(2022, 1, 1)) == 2021
    assert xbrl.fiscal_year_for(date(2026, 1, 25)) == 2026


# ---------------------------------------------------------------- tool, from recorded fixtures


@pytest.fixture(autouse=True)
async def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Agent test@example.test")
    cache_mod.set_default_cache(Cache(tmp_path / "cache.db"))
    reset_limiters()
    yield
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


@pytest.fixture
def sec():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        tickers = load_tickers()
        mock.get(edgar.TICKERS_URL).mock(return_value=httpx.Response(200, json=tickers))
        for cik in CIKS.values():
            path = SEC / "companyfacts" / f"CIK{edgar.pad_cik(cik)}.json.gz"
            mock.get(xbrl.companyfacts_url(cik)).mock(
                return_value=httpx.Response(200, content=gzip.decompress(path.read_bytes()))
            )
        yield mock


async def test_bank_revenue_and_missing_metrics(sec):
    res = await xbrl.get_fundamentals(
        "JPM", metrics=["revenue", "gross_profit", "operating_income"], n=3
    )
    rev = [v for v in res.values if v.metric == "revenue"]
    assert [v.fiscal_year for v in rev] == [2025, 2024, 2023]
    assert all(v.tag in ("Revenues", "RevenuesNetOfInterestExpense") for v in rev)
    assert rev[0].value == 182_447_000_000
    for metric in ("gross_profit", "operating_income"):
        vals = [v for v in res.values if v.metric == metric]
        assert vals and all(v.value is None for v in vals)
        assert all(v.reason == "no matching us-gaap tag" for v in vals)


async def test_source_fields(sec):
    res = await xbrl.get_fundamentals("MSFT", metrics=["revenue", "diluted_eps"], n=2)
    assert res.source == "SEC EDGAR XBRL companyfacts"
    assert res.source_url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"
    assert res.cik == "0000789019" and res.ticker == "MSFT"
    assert res.as_of == max(v.filed for v in res.values)
    assert res.retrieved_at.tzinfo is not None
    eps = next(v for v in res.values if v.metric == "diluted_eps" and v.fiscal_year == 2026)
    assert eps.unit == "USD/shares" and eps.value == 17.95
    assert eps.accession == "0001193125-26-323660" and eps.filed == date(2026, 7, 29)
    assert eps.source_url == ("https://www.sec.gov/Archives/edgar/data/789019/000119312526323660/")
    assert eps.period_start == date(2025, 7, 1) and eps.period_end == date(2026, 6, 30)
    assert eps.derived is False
    xbrl.Fundamentals.model_validate(res.model_dump(mode="json"))


async def test_quarterly_only_true_quarters(sec):
    res = await xbrl.get_fundamentals("MSFT", metrics=["revenue"], periods="quarterly", n=3)
    got = [(v.period_end, v.value, v.fiscal_year, v.fiscal_period) for v in res.values]
    # Q4 (Apr-Jun) is never reported as a 3-month fact, so it is absent until T9 derives it.
    assert got == [
        (date(2026, 3, 31), 82_886_000_000, 2026, "Q3"),
        (date(2025, 12, 31), 81_273_000_000, 2026, "Q2"),
        (date(2025, 9, 30), 77_673_000_000, 2026, "Q1"),
    ]
    for v in res.values:
        assert 80 <= (v.period_end - v.period_start).days <= 100


async def test_shares_outstanding_from_cover_page(sec):
    res = await xbrl.get_fundamentals("MSFT", metrics=["shares_outstanding"], n=1)
    (v,) = res.values
    assert v.tag == "dei:EntityCommonStockSharesOutstanding"
    assert v.value == 7_425_545_491 and v.period_end == date(2026, 7, 23)
    assert v.fiscal_year == 2026 and v.unit == "shares"


async def test_bad_input(sec):
    with pytest.raises(BadInput):
        await xbrl.get_fundamentals("MSFT", metrics=["ebitda"])
    with pytest.raises(BadInput):
        await xbrl.get_fundamentals("MSFT", n=0)
    with pytest.raises(BadInput):
        await xbrl.get_fundamentals("MSFT", periods="monthly")  # type: ignore[arg-type]


async def test_cover_share_count_dated_on_period_end_stays_with_that_period(sec):
    # JPM dates its 10-Q cover share count on the quarter end itself.
    res = await xbrl.get_fundamentals(
        "JPM", metrics=["shares_outstanding"], periods="quarterly", n=2
    )
    q2, q1 = res.values
    assert q2.fiscal_period == "Q2" and q2.period_end == date(2026, 6, 30)
    assert q2.value == 2_658_186_195
    assert q1.fiscal_period == "Q1" and q1.period_end < date(2026, 6, 30)
