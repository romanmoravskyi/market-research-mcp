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
CIKS = {
    "MSFT": "789019",
    "JPM": "19617",
    "O": "726728",
    "NVDA": "1045810",
    "AAPL": "320193",
    "BRK-B": "1067983",
}


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


async def test_quarterly_reported_quarters_plus_derived_q4(sec):
    res = await xbrl.get_fundamentals("MSFT", metrics=["revenue"], periods="quarterly", n=4)
    got = [(v.period_end, v.value, v.fiscal_year, v.fiscal_period, v.derived) for v in res.values]
    # Q4 (Apr-Jun) is never reported as a 3-month fact: FY 10-K 0001193125-26-323660
    # ($331,839M) minus 9M YTD 10-Q 0001193125-26-191507 ($241,832M) = $90,007M.
    assert got == [
        (date(2026, 6, 30), 90_007_000_000, 2026, "Q4", True),
        (date(2026, 3, 31), 82_886_000_000, 2026, "Q3", False),
        (date(2025, 12, 31), 81_273_000_000, 2026, "Q2", False),
        (date(2025, 9, 30), 77_673_000_000, 2026, "Q1", False),
    ]
    for v in res.values:
        assert 80 <= (v.period_end - v.period_start).days <= 100
    q4 = res.values[0]
    assert q4.period_start == date(2026, 4, 1)
    assert "FY - 9M YTD" in q4.note and "0001193125-26-191507" in q4.note
    assert q4.accession == "0001193125-26-323660"
    assert any("FY - 9M YTD" in n for n in res.notes)
    assert not any("not derived yet" in n for n in res.notes)


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


@pytest.mark.parametrize(
    ("end", "label"),
    [
        (date(2021, 9, 30), (2022, "Q1")),  # older than every annual period
        (date(2022, 3, 31), (2022, "Q3")),
        (date(2019, 6, 30), (2019, "Q4")),
        (date(2025, 3, 31), (2025, "Q3")),  # inside the known annual periods
        (date(2026, 12, 31), (2027, "Q2")),  # newer than every annual period
    ],
)
def test_fiscal_label_extrapolates_outside_annual_periods(end, label):
    fy_ends = [date(2024, 6, 30), date(2025, 6, 30)]
    assert xbrl._fiscal_label(end, "quarterly", fy_ends) == label


# ---------------------------------------------------------------- derived quarters (T9)


def _cal_year(concept: str, values: dict[str, float], *, unit: str = "USD") -> list[Fact]:
    """Calendar-2024 facts keyed by period label: Q1..Q4 (3 months), 6M, 9M, FY."""
    periods = {
        "Q1": ("2024-01-01", "2024-03-31", "10-Q", "2024-04-30", "0000000001-24-000001"),
        "Q2": ("2024-04-01", "2024-06-30", "10-Q", "2024-07-30", "0000000001-24-000002"),
        "6M": ("2024-01-01", "2024-06-30", "10-Q", "2024-07-30", "0000000001-24-000002"),
        "Q3": ("2024-07-01", "2024-09-30", "10-Q", "2024-10-30", "0000000001-24-000003"),
        "9M": ("2024-01-01", "2024-09-30", "10-Q", "2024-10-30", "0000000001-24-000003"),
        "FY": ("2024-01-01", "2024-12-31", "10-K", "2025-02-15", "0000000001-25-000001"),
    }
    out = []
    for label, value in values.items():
        start, end, form, filed, accession = periods[label]
        out.append(
            fact(concept, start, end, value, form=form, filed=filed, accession=accession, unit=unit)
        )
    return out


def _quarters(vals, metric):
    return {v.fiscal_period: v for v in vals if v.metric == metric and v.fiscal_year == 2024}


def test_q4_derived_as_fy_minus_9m_ytd():
    facts = _cal_year("Revenues", {"Q1": 10, "Q2": 15, "6M": 25, "Q3": 20, "9M": 45, "FY": 70})
    vals = xbrl.normalize(facts, cik="1", metrics=["revenue"], periods="quarterly", n=4)
    q = _quarters(vals, "revenue")
    assert [q[p].value for p in ("Q1", "Q2", "Q3", "Q4")] == [10, 15, 20, 25]
    assert not q["Q1"].derived and not q["Q3"].derived
    q4 = q["Q4"]
    assert q4.derived is True
    assert q4.period_start == date(2024, 10, 1) and q4.period_end == date(2024, 12, 31)
    assert q4.note and "FY - 9M YTD" in q4.note
    assert "0000000001-25-000001" in q4.note and "0000000001-24-000003" in q4.note
    # The derived value points at the latest filing used (the 10-K).
    assert q4.accession == "0000000001-25-000001" and q4.form == "10-K"


def test_q4_falls_back_to_fy_minus_three_quarters():
    facts = _cal_year("Revenues", {"Q1": 10, "Q2": 15, "Q3": 20, "FY": 70})
    vals = xbrl.normalize(facts, cik="1", metrics=["revenue"], periods="quarterly", n=4)
    q4 = _quarters(vals, "revenue")["Q4"]
    assert q4.value == 25 and q4.derived
    assert "FY - Q1 - Q2 - Q3" in q4.note


def test_cash_flow_quarters_derived_from_ytd():
    facts = _cal_year(
        "NetCashProvidedByUsedInOperatingActivities",
        {"Q1": 30, "6M": 54, "9M": 82, "FY": 111},
    )
    vals = xbrl.normalize(facts, cik="1", metrics=["operating_cash_flow"], periods="quarterly", n=4)
    q = _quarters(vals, "operating_cash_flow")
    assert [q[p].value for p in ("Q1", "Q2", "Q3", "Q4")] == [30, 24, 28, 29]
    assert [q[p].derived for p in ("Q1", "Q2", "Q3", "Q4")] == [False, True, True, True]
    assert "6M YTD - 3M" in q["Q2"].note
    assert "9M YTD - 6M YTD" in q["Q3"].note
    assert q["Q2"].period_start == date(2024, 4, 1)


def test_direct_quarter_wins_over_derivation():
    facts = _cal_year("Revenues", {"Q1": 10, "Q2": 16, "6M": 25, "FY": 70})  # Q2 direct
    vals = xbrl.normalize(facts, cik="1", metrics=["revenue"], periods="quarterly", n=4)
    q2 = _quarters(vals, "revenue")["Q2"]
    assert q2.value == 16 and not q2.derived


def test_per_share_and_share_count_q4_not_derived():
    facts = _cal_year("Revenues", {"Q1": 10, "Q2": 15, "Q3": 20, "9M": 45, "FY": 70})
    facts += _cal_year(
        "EarningsPerShareDiluted", {"Q1": 1.0, "Q2": 1.5, "Q3": 2.0, "FY": 7.0}, unit="USD/shares"
    )
    vals = xbrl.normalize(facts, cik="1", metrics=["diluted_eps"], periods="quarterly", n=4)
    q4 = _quarters(vals, "diluted_eps")["Q4"]
    assert q4.value is None
    assert "not additive" in q4.reason


def test_annual_values_unaffected_by_derivation():
    facts = _cal_year("Revenues", {"Q1": 10, "6M": 25, "9M": 45, "FY": 70})
    vals = xbrl.normalize(facts, cik="1", metrics=["revenue"], periods="annual", n=4)
    assert [(v.value, v.derived) for v in vals] == [(70, False)]


# ---------------------------------------------------------------- splits (T9)


def _split_case() -> list[Fact]:
    """A 10:1 split between filings of 2024-05-01 (old basis) and 2024-08-01 (new basis)."""
    old, new = "0000000001-24-000002", "0000000001-24-000003"
    sh, eps = "shares", "USD/shares"
    w = "WeightedAverageNumberOfDilutedSharesOutstanding"
    e = "EarningsPerShareDiluted"
    return [
        # FY2022: first reported pre-split, later restated by the company on the new basis.
        fact(w, "2022-01-01", "2022-12-31", 1000, filed="2023-02-01", unit=sh),
        fact(w, "2022-01-01", "2022-12-31", 10000, filed="2025-02-01", accession=new, unit=sh),
        fact(e, "2022-01-01", "2022-12-31", 3.0, filed="2023-02-01", unit=eps),
        fact(e, "2022-01-01", "2022-12-31", 0.3, filed="2025-02-01", accession=new, unit=eps),
        # Q1 2024: reported pre-split, restated in the next year's 10-Q.
        fact(
            w,
            "2024-01-01",
            "2024-03-31",
            990,
            filed="2024-05-01",
            form="10-Q",
            accession=old,
            unit=sh,
        ),
        fact(
            w,
            "2024-01-01",
            "2024-03-31",
            9900,
            filed="2024-08-01",
            form="10-Q",
            accession=new,
            unit=sh,
        ),
        # FY2023: only ever reported pre-split (never restated).
        fact(w, "2023-01-01", "2023-12-31", 1010, filed="2024-02-01", unit=sh),
        fact(e, "2023-01-01", "2023-12-31", 2.5, filed="2024-02-01", unit=eps),
        fact("NetIncomeLoss", "2023-01-01", "2023-12-31", 2525, filed="2024-02-01"),
        fact("NetIncomeLoss", "2022-01-01", "2022-12-31", 3000, filed="2023-02-01"),
    ]


def test_detect_split_from_restated_share_counts():
    (s,) = xbrl.detect_splits(_split_case())
    assert s.ratio == 10
    assert s.after == date(2024, 5, 1) and s.before == date(2024, 8, 1)
    assert s.evidence >= 2


def test_no_split_from_small_restatement_or_single_pair():
    w = "WeightedAverageNumberOfDilutedSharesOutstanding"
    facts = [
        fact(w, "2022-01-01", "2022-12-31", 1000, filed="2023-02-01", unit="shares"),
        fact(w, "2022-01-01", "2022-12-31", 1020, filed="2024-02-01", unit="shares"),
        fact(w, "2021-01-01", "2021-12-31", 500, filed="2022-02-01", unit="shares"),
        fact(w, "2021-01-01", "2021-12-31", 1000, filed="2024-02-01", unit="shares"),  # one pair
    ]
    assert xbrl.detect_splits(facts) == []


def test_pre_split_values_adjusted_and_restated_values_kept():
    vals = xbrl.normalize(_split_case(), cik="1", metrics=["shares_diluted", "diluted_eps"], n=2)
    got = {(v.fiscal_year, v.metric): v for v in vals}
    fy23_sh, fy23_eps = got[(2023, "shares_diluted")], got[(2023, "diluted_eps")]
    assert fy23_sh.value == 10100 and fy23_sh.split_factor == 10
    assert fy23_eps.value == 0.25 and fy23_eps.split_factor == 10
    assert "split-adjusted" in fy23_sh.note and "10:1" in fy23_sh.note
    # The company's own restated figures win over adjusting; no adjustment noted.
    fy22 = got[(2022, "shares_diluted")]
    assert fy22.value == 10000 and fy22.split_factor == 1 and fy22.note is None


def test_explicit_splits_override_detection():
    split = xbrl.SplitEvent(ratio=2, after=date(2024, 1, 1), before=date(2024, 3, 1), evidence=0)
    vals = xbrl.normalize(_split_case(), cik="1", metrics=["diluted_eps"], n=1, splits=[split])
    assert vals[0].value == 1.25 and vals[0].split_factor == 2


async def test_nvda_splits_detected_from_companyfacts(sec):
    res = await xbrl.get_fundamentals("NVDA", metrics=["shares_diluted"], n=1)
    got = [(s.ratio, s.after, s.before) for s in res.splits]
    assert [r for r, _, _ in got] == [4, 10]
    # The real split dates (4:1 on 2021-07-20, 10:1 on 2024-06-10) lie in each window.
    assert got[0][1] < date(2021, 7, 20) <= got[0][2]
    assert got[1][1] < date(2024, 6, 10) <= got[1][2]


def test_unit_scale_errors_are_not_splits():
    # Filers sometimes tag a value in thousands, then fix it: a 1000x "restatement".
    w = "WeightedAverageNumberOfDilutedSharesOutstanding"
    facts = [
        fact(w, f"{y}-01-01", f"{y}-12-31", v, filed=filed, unit="shares")
        for y in (2020, 2021)
        for v, filed in ((1_000, "2022-02-01"), (1_000_000, "2022-05-01"))
    ]
    assert xbrl.detect_splits(facts) == []


async def test_aapl_splits_detected_without_scale_noise(sec):
    res = await xbrl.get_fundamentals("AAPL", metrics=["shares_diluted"], n=1)
    assert [s.ratio for s in res.splits] == [7, 4]  # 7:1 June 2014, 4:1 August 2020
