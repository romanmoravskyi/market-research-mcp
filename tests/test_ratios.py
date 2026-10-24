"""get_key_ratios: ratios computed from SEC XBRL fundamentals (recorded companyfacts).

Inputs are the 10-K figures pinned in tests/test_xbrl_golden.py, plus balance-sheet values
from the same 10-Ks (AAPL FY2025 10-K 0000320193-25-000079: total assets $359,241M,
current assets $147,957M, current liabilities $165,631M, shareholders' equity $73,733M,
term debt $78,328M non-current + $12,350M current, operating cash flow $111,482M, capex
$12,715M). Expected ratios are recomputed here from those figures, rounded to 4 places.
"""

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
from market_research_mcp.config import ConfigError
from market_research_mcp.errors import BadInput
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import edgar, ratios, xbrl
from market_research_mcp.sources.prices import Split
from market_research_mcp.sources.xbrl import Fact

SEC = Path(__file__).parent / "fixtures" / "sec"
CIKS = {
    "MSFT": "789019",
    "AAPL": "320193",
    "BRK-B": "1067983",
    "JPM": "19617",
    "O": "726728",
    "NVDA": "1045810",
}
# Real submissions fixtures exist for MSFT, JPM, O and BRK-B. For AAPL and NVDA a minimal
# synthetic submissions body carries only their real SEC SIC codes.
SYNTHETIC_SUBMISSIONS = {"AAPL": {"sic": "3571"}, "NVDA": {"sic": "3674"}}


def load_gz(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


@pytest.fixture(autouse=True)
async def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Test Agent test@example.test")
    cache_mod.set_default_cache(Cache(tmp_path / "cache.db"))
    reset_limiters()
    yield
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


@pytest.fixture(autouse=True)
def no_splits_since_filing(monkeypatch):
    """Price-provider split lookups need a key; tests stub them (default: no splits)."""
    calls = []

    async def lookup(ticker, start, end):
        calls.append((ticker, start, end))
        return []

    monkeypatch.setattr(ratios, "split_lookup", lookup)
    return calls


@pytest.fixture
def sec():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        tickers = json.loads(load_gz(SEC / "company_tickers_exchange.json.gz"))
        mock.get(edgar.TICKERS_URL).mock(return_value=httpx.Response(200, json=tickers))
        for ticker, cik in CIKS.items():
            facts = SEC / "companyfacts" / f"CIK{edgar.pad_cik(cik)}.json.gz"
            mock.get(xbrl.companyfacts_url(cik)).mock(
                return_value=httpx.Response(200, content=load_gz(facts))
            )
            sub = SEC / f"CIK{edgar.pad_cik(cik)}.json.gz"
            body = load_gz(sub) if sub.exists() else json.dumps(SYNTHETIC_SUBMISSIONS[ticker])
            mock.get(edgar.submissions_url(cik)).mock(
                return_value=httpx.Response(200, content=body)
            )
        yield mock


def by_key(res):
    return {(r.fiscal_year, r.ratio): r for r in res.ratios}


async def test_aapl_annual_ratios(sec):
    res = await ratios.get_key_ratios("AAPL", n=2)
    assert res.industry == "general" and "3571" in res.industry_basis
    got = by_key(res)
    expected = {
        "gross_margin": 195_201 / 416_161,
        "operating_margin": 133_050 / 416_161,
        "net_margin": 112_010 / 416_161,
        "roe": 112_010 / 73_733,
        "roa": 112_010 / 359_241,
        "debt_to_equity": (78_328 + 12_350) / 73_733,
        "current_ratio": 147_957 / 165_631,
        "fcf_margin": (111_482 - 12_715) / 416_161,
    }
    for name, value in expected.items():
        r = got[(2025, name)]
        assert r.value == round(value, 4), name
        assert r.reason is None
        assert r.source == "SEC EDGAR XBRL companyfacts"
        assert r.source_url.startswith("https://www.sec.gov/Archives/edgar/data/320193/")
        assert r.as_of is not None and r.formula
        assert r.period == "FY2025" and r.period_end == date(2025, 9, 27)
    assert got[(2025, "fcf_margin")].inputs == {
        "operating_cash_flow": 111_482_000_000,
        "capex": 12_715_000_000,
        "revenue": 416_161_000_000,
    }
    assert {r.fiscal_year for r in res.ratios} == {2025, 2024}
    ratios.KeyRatios.model_validate(res.model_dump(mode="json"))


async def test_pe_null_without_price_and_only_latest_with_price(sec):
    res = await ratios.get_key_ratios("AAPL", n=2)
    pe = by_key(res)[(2025, "pe")]
    assert pe.value is None and "price" in pe.reason

    res = await ratios.get_key_ratios("AAPL", n=2, price=250.0, price_date=date(2026, 10, 23))
    got = by_key(res)
    assert got[(2025, "pe")].value == round(250.0 / 7.46, 4)
    assert "caller" in got[(2025, "pe")].source and "2026-10-23" in got[(2025, "pe")].source
    assert got[(2024, "pe")].value is None and "latest" in got[(2024, "pe")].reason


async def test_bank_ratios_that_do_not_apply_are_null(sec):
    res = await ratios.get_key_ratios("JPM", n=1)
    assert res.industry == "bank" and "6021" in res.industry_basis
    got = by_key(res)
    for name in (
        "gross_margin",
        "operating_margin",
        "current_ratio",
        "debt_to_equity",
        "fcf_margin",
    ):
        r = got[(2025, name)]
        assert r.value is None and "bank" in r.reason.lower(), name
    # FY2025 10-K 0001628280-26-008131: net income $57,048M, total net revenue $182,447M,
    # stockholders' equity $362,438M, total assets $4,424,900M.
    assert got[(2025, "net_margin")].value == round(57_048 / 182_447, 4)
    assert got[(2025, "roe")].value == round(57_048 / 362_438, 4)
    assert got[(2025, "roa")].value == round(57_048 / 4_424_900, 4)


async def test_reit_ratios_that_do_not_apply_are_null(sec):
    res = await ratios.get_key_ratios("O", n=1, price=60.0, price_date=date(2026, 10, 23))
    assert res.industry == "reit"
    got = by_key(res)
    for name in ("gross_margin", "current_ratio", "fcf_margin", "pe"):
        r = got[(2025, name)]
        assert r.value is None and "reit" in r.reason.lower(), name
    # FY2025 10-K 0000726728-26-000011: net income $1,058,590K on revenue $5,749,377K;
    # stockholders' equity $39,438,695K.
    assert got[(2025, "net_margin")].value == round(1_058_590 / 5_749_377, 4)
    assert got[(2025, "roe")].value == round(1_058_590 / 39_438_695, 4)


async def test_insurer_gross_margin_and_current_ratio_null(sec):
    res = await ratios.get_key_ratios("BRK-B", n=1)
    assert res.industry == "insurer"
    got = by_key(res)
    for name in ("gross_margin", "current_ratio"):
        assert got[(2025, name)].value is None and "insur" in got[(2025, name)].reason.lower()
    # FY2025 10-K 0001193125-26-083899: net earnings attributable $66,968M; shareholders'
    # equity $717,419M.
    assert got[(2025, "roe")].value == round(66_968 / 717_419, 4)


async def test_missing_input_gives_reason_not_number(sec):
    # Realty Income tags no LongTermDebt* concepts, so total_debt (and D/E) is unavailable.
    res = await ratios.get_key_ratios("O", n=1)
    de = by_key(res)[(2025, "debt_to_equity")]
    assert de.value is None and "total_debt" in de.reason


async def test_quarterly_ratios_use_derived_quarters(sec):
    res = await ratios.get_key_ratios("AAPL", periods="quarterly", n=4)
    got = {(r.fiscal_year, r.period, r.ratio): r for r in res.ratios}
    fcf = got[(2025, "FY2025 Q4", "fcf_margin")]
    # Q4 FY2025, all inputs derived: OCF 111,482 - 81,754; capex 12,715 - 9M YTD capex;
    # revenue 416,161 - 313,695.
    assert fcf.derived is True and fcf.value is not None
    roe = got[(2025, "FY2025 Q4", "roe")]
    assert "not annualized" in roe.formula
    pe = got[(2025, "FY2025 Q4", "pe")]
    assert pe.value is None and "annual" in pe.reason


async def test_industry_falls_back_to_facts_when_submissions_unavailable(sec):
    sec.get(edgar.submissions_url("19617")).mock(return_value=httpx.Response(404))
    res = await ratios.get_key_ratios("JPM", n=1)
    assert res.industry == "bank" and "Deposits" in res.industry_basis


async def test_bad_input(sec):
    with pytest.raises(BadInput):
        await ratios.get_key_ratios("AAPL", n=0)
    with pytest.raises(BadInput):
        await ratios.get_key_ratios("AAPL", periods="monthly")  # type: ignore[arg-type]
    with pytest.raises(BadInput):
        await ratios.get_key_ratios("AAPL", price=-1.0)


def _fact(concept, start, end, value, unit="USD"):
    return Fact(
        concept=concept,
        unit=unit,
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end),
        value=value,
        accession="0000000001-25-000001",
        form="10-K",
        filed=date(2025, 2, 1),
    )


def test_negative_equity_and_revenue_guarded():
    facts = [
        _fact("Revenues", "2024-01-01", "2024-12-31", 0),
        _fact("NetIncomeLoss", "2024-01-01", "2024-12-31", 10),
        _fact("StockholdersEquity", None, "2024-12-31", -5),
    ]
    out = ratios.compute(facts, cik="1", industry="general", periods="annual", n=1)
    got = {r.ratio: r for r in out}
    assert got["net_margin"].value is None and "revenue" in got["net_margin"].reason
    assert got["roe"].value is None and "equity" in got["roe"].reason


# ---------------------------------------------------------------- review fixes (PR #7)


async def test_price_requires_price_date(sec):
    with pytest.raises(BadInput):
        await ratios.get_key_ratios("AAPL", price=250.0)


async def test_pe_adjusts_eps_for_split_after_last_filing(sec, monkeypatch):
    # AAPL FY2025 EPS $7.46 was filed 2025-10-31; a (hypothetical) 4:1 split afterwards puts
    # the price on a basis 4x smaller than the EPS.
    async def lookup(ticker, start, end):
        return [Split(date=date(2026, 9, 1), factor=4.0)]

    monkeypatch.setattr(ratios, "split_lookup", lookup)
    res = await ratios.get_key_ratios("AAPL", n=1, price=62.5, price_date=date(2026, 10, 23))
    pe = by_key(res)[(2025, "pe")]
    assert pe.value == round(62.5 / (7.46 / 4), 4)
    assert "4:1" in pe.note and "2026-09-01" in pe.note
    assert pe.inputs["diluted_eps"] == round(7.46 / 4, 6)


async def test_pe_null_when_split_check_unavailable(sec, monkeypatch):
    async def lookup(ticker, start, end):
        raise ConfigError("TIINGO_API_KEY is not set")

    monkeypatch.setattr(ratios, "split_lookup", lookup)
    res = await ratios.get_key_ratios("AAPL", n=1, price=250.0, price_date=date(2026, 10, 23))
    pe = by_key(res)[(2025, "pe")]
    assert pe.value is None and "split" in pe.reason


async def test_pe_no_split_check_when_price_not_after_filing(sec, no_splits_since_filing):
    await ratios.get_key_ratios("AAPL", n=1, price=250.0, price_date=date(2025, 10, 31))
    assert no_splits_since_filing == []


async def test_pe_split_already_in_sec_data_not_applied_twice(sec, monkeypatch):
    # NVDA's June 2024 10:1 split is already reflected in its FY2026 EPS ($4.90).
    async def lookup(ticker, start, end):
        return [Split(date=date(2024, 6, 10), factor=10.0)]

    monkeypatch.setattr(ratios, "split_lookup", lookup)
    res = await ratios.get_key_ratios("NVDA", n=1, price=180.0, price_date=date(2026, 10, 23))
    assert by_key(res)[(2026, "pe")].value == round(180.0 / 4.9, 4)


def test_insurance_agents_are_not_insurers():
    assert ratios.classify_industry("6411", [])[0] == "general"
    assert ratios.classify_industry("6331", [])[0] == "insurer"
