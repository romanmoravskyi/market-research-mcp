"""Key financial ratios computed from SEC XBRL fundamentals (`get_key_ratios`).

Every ratio is computed from `xbrl.normalize` values for one period (no estimates), carries
its formula, inputs, source filing and date, and is null with a reason when an input is
missing, a denominator is zero or negative, or the ratio does not apply to the company's
industry (banks, insurers, REITs). Descriptive only: no scores, signals or targets.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from market_research_mcp.errors import BadInput, ToolError
from market_research_mcp.models import Sourced
from market_research_mcp.sources import edgar, prices, xbrl

__all__ = [
    "RATIOS",
    "KeyRatios",
    "RatioValue",
    "classify_industry",
    "compute",
    "get_key_ratios",
    "register",
]

logger = logging.getLogger(__name__)

Industry = Literal["general", "bank", "insurer", "reit"]
SOURCE = xbrl.SOURCE
DECIMALS = 4
MAX_N = 20


@dataclass(frozen=True)
class RatioSpec:
    formula: str
    numerator: tuple[str, ...]  # metrics combined by `combine`
    denominator: str
    combine: Callable[[list[float]], float] = lambda xs: xs[0]
    positive_denominator: str = ""  # reason when the denominator is <= 0


def _fcf(xs: list[float]) -> float:
    return xs[0] - xs[1]


REVENUE_GUARD = "revenue is zero or negative; ratio not meaningful"
EQUITY_GUARD = "stockholders' equity is zero or negative; ratio not meaningful"

RATIOS: dict[str, RatioSpec] = {
    "gross_margin": RatioSpec(
        "gross_profit / revenue", ("gross_profit",), "revenue", positive_denominator=REVENUE_GUARD
    ),
    "operating_margin": RatioSpec(
        "operating_income / revenue",
        ("operating_income",),
        "revenue",
        positive_denominator=REVENUE_GUARD,
    ),
    "net_margin": RatioSpec(
        "net_income / revenue", ("net_income",), "revenue", positive_denominator=REVENUE_GUARD
    ),
    "roe": RatioSpec(
        "net_income / stockholders_equity (period-end equity)",
        ("net_income",),
        "stockholders_equity",
        positive_denominator=EQUITY_GUARD,
    ),
    "roa": RatioSpec(
        "net_income / total_assets (period-end assets)",
        ("net_income",),
        "total_assets",
        positive_denominator="total assets are zero or negative; ratio not meaningful",
    ),
    "debt_to_equity": RatioSpec(
        "total_debt / stockholders_equity (total_debt excludes leases)",
        ("total_debt",),
        "stockholders_equity",
        positive_denominator=EQUITY_GUARD,
    ),
    "current_ratio": RatioSpec(
        "current_assets / current_liabilities",
        ("current_assets",),
        "current_liabilities",
        positive_denominator="current liabilities are zero or negative; ratio not meaningful",
    ),
    "fcf_margin": RatioSpec(
        "(operating_cash_flow - capex) / revenue",
        ("operating_cash_flow", "capex"),
        "revenue",
        combine=_fcf,
        positive_denominator=REVENUE_GUARD,
    ),
}
PE = "pe"
PE_FORMULA = "price / diluted_eps (latest fiscal year)"
RATIO_NAMES = [*RATIOS, PE]

NOT_APPLICABLE: dict[str, dict[str, str]] = {
    "bank": {
        "gross_margin": "not applicable to banks: no cost of goods sold, so no gross profit",
        "operating_margin": "not applicable to banks: banks do not report operating income; "
        "see net_margin",
        "current_ratio": "not applicable to banks: bank balance sheets are unclassified "
        "(no current assets or current liabilities)",
        "debt_to_equity": "not applicable to banks: funding is mostly deposits, so "
        "debt/equity is not comparable with other industries",
        "fcf_margin": "not applicable to banks: bank operating cash flow swings with trading "
        "and lending balances, so free cash flow is not meaningful",
    },
    "insurer": {
        "gross_margin": "not applicable to insurers: no cost of goods sold, so no gross profit",
        "current_ratio": "not applicable to insurers: insurer balance sheets are unclassified "
        "(no current assets or current liabilities)",
    },
    "reit": {
        "gross_margin": "not applicable to REITs: REITs do not report gross profit",
        "current_ratio": "not applicable to REITs: REIT balance sheets are unclassified "
        "(no current assets or current liabilities)",
        "fcf_margin": "not applicable to REITs: property acquisitions are not capex, and FFO/"
        "AFFO (not in us-gaap XBRL) are the usual cash-earnings measures",
        PE: "not applicable to REITs: real-estate depreciation distorts EPS; price/FFO is the "
        "usual measure and FFO is not in us-gaap XBRL",
    },
}

# SEC SIC codes: 6020-6036 commercial banks and savings institutions; 6311-6399 insurance
# carriers; 6798 real estate investment trusts.
BANK_SIC = range(6020, 6037)
INSURER_SIC = range(6311, 6400)  # 6411 agents/brokers excluded
REIT_SIC = 6798

NOTES = [
    "Ratios are computed from SEC XBRL values for the same period (see get_fundamentals); "
    "nothing is estimated. Rounded to 4 decimals; 0.25 = 25%.",
    "A ratio is null with a reason when an input is missing, a denominator is zero or "
    "negative, or the ratio does not apply to the industry (banks, insurers, REITs).",
    "Quarterly ROE and ROA use one quarter's net income and are not annualized.",
    "P/E puts EPS on the price's share basis: splits between the EPS filing and price_date "
    "come from the configured price provider; if that check fails, P/E is null.",
    "Descriptive figures only; not investment advice and not a buy/sell signal.",
]


class RatioValue(BaseModel):
    ratio: str
    value: float | None
    period: str  # "FY2025" or "FY2026 Q3"
    period_start: date | None = None
    period_end: date
    fiscal_year: int
    fiscal_period: str | None = None
    formula: str
    inputs: dict[str, int | float | None] = {}
    derived: bool = False
    source: str
    source_url: str | None = None
    as_of: date | None = None
    note: str | None = None
    reason: str | None = None


class KeyRatios(Sourced):
    ticker: str
    cik: str
    name: str
    periods: xbrl.Periods
    industry: Industry
    industry_basis: str
    ratios: list[RatioValue]
    splits: list[xbrl.SplitEvent] = []


# ---------------------------------------------------------------- classification


def classify_industry(sic: str | int | None, facts: list[xbrl.Fact]) -> tuple[Industry, str]:
    """Industry from the SEC SIC code; without one, from tell-tale XBRL tags."""
    code: int | None
    try:
        code = int(str(sic)) if sic not in (None, "") else None
    except ValueError:
        code = None
    if code is not None:
        basis = f"SIC {code} (SEC submissions)"
        if code in BANK_SIC:
            return "bank", basis
        if code in INSURER_SIC:
            return "insurer", basis
        if code == REIT_SIC:
            return "reit", basis
        return "general", basis
    concepts = {f.concept for f in facts}
    if "Deposits" in concepts:
        return "bank", "heuristic: us-gaap Deposits tag present (SIC unavailable)"
    if "PremiumsEarnedNet" in concepts:
        return "insurer", "heuristic: us-gaap PremiumsEarnedNet tag present (SIC unavailable)"
    return "general", "SIC unavailable; no bank or insurer tags found"


# ---------------------------------------------------------------- computation


def _metrics_needed() -> list[str]:
    names = {"diluted_eps"}
    for spec in RATIOS.values():
        names.update(spec.numerator)
        names.add(spec.denominator)
    return [m for m in xbrl.METRICS if m in names]


def _period_label(v: xbrl.FundamentalValue) -> str:
    if v.fiscal_period in (None, "FY"):
        return f"FY{v.fiscal_year}"
    return f"FY{v.fiscal_year} {v.fiscal_period}"


def _round(x: float) -> float:
    return round(x, DECIMALS)


def _sourced(inputs: list[xbrl.FundamentalValue]) -> dict:
    present = [v for v in inputs if v.filed is not None]
    latest = max(present, key=lambda v: (v.filed, v.accession or ""), default=None)
    notes = [f"{v.metric}: {v.note}" for v in inputs if v.note]
    return {
        "source_url": latest.source_url if latest else None,
        "as_of": latest.filed if latest else None,
        "derived": any(v.derived for v in inputs),
        "note": "; ".join(notes) or None,
    }


def compute(
    facts: list[xbrl.Fact],
    *,
    cik: str,
    industry: Industry,
    periods: xbrl.Periods = "annual",
    n: int = 4,
    price: float | None = None,
    price_date: date | None = None,
    splits: list[xbrl.SplitEvent] | None = None,
    price_splits: list[prices.Split] | None = None,
    price_split_error: str | None = None,
) -> list[RatioValue]:
    """Ratios for the newest `n` periods (newest first), one row per (period, ratio).

    `price_splits` are splits from a price provider; those after the EPS filing and on or
    before `price_date` (and not already in the SEC data) put EPS on the price's basis.
    `price_split_error` (the lookup failed) makes P/E null.
    """
    if splits is None:
        splits = xbrl.detect_splits(facts)
    values = xbrl.normalize(
        facts, cik=cik, metrics=_metrics_needed(), periods=periods, n=n, splits=splits
    )
    by_end: dict[date, dict[str, xbrl.FundamentalValue]] = {}
    anchor: dict[date, xbrl.FundamentalValue] = {}
    for v in values:
        by_end.setdefault(v.period_end, {})[v.metric] = v
        if v.metric == "revenue" or v.period_end not in anchor:
            anchor[v.period_end] = v
    ends = sorted(by_end, reverse=True)
    na = NOT_APPLICABLE.get(industry, {})
    out: list[RatioValue] = []
    for i, end in enumerate(ends):
        row = by_end[end]
        a = anchor[end]
        start = next((v.period_start for v in row.values() if v.period_start), None)
        base = {
            "period": _period_label(a),
            "period_start": start,
            "period_end": end,
            "fiscal_year": a.fiscal_year,
            "fiscal_period": a.fiscal_period,
        }
        for name, spec in RATIOS.items():
            formula = spec.formula
            if periods == "quarterly" and name in ("roe", "roa"):
                formula += "; quarterly net income, not annualized"
            out.append(_ratio(name, spec, formula, row, na.get(name), base))
        pe_ctx = _PriceContext(price, price_date, splits, price_splits or [], price_split_error)
        out.append(_pe(row, na.get(PE), base, periods, i == 0, pe_ctx))
    return out


def _ratio(
    name: str,
    spec: RatioSpec,
    formula: str,
    row: dict[str, xbrl.FundamentalValue],
    not_applicable: str | None,
    base: dict,
) -> RatioValue:
    r = RatioValue(ratio=name, value=None, formula=formula, source=SOURCE, **base)
    if not_applicable:
        r.reason = not_applicable
        return r
    used = [*spec.numerator, spec.denominator]
    inputs = [row[m] for m in used if m in row]
    r.inputs = {v.metric: v.value for v in inputs}
    for k, val in _sourced(inputs).items():
        setattr(r, k, val)
    missing = [m for m in used if row.get(m) is None or row[m].value is None]
    if missing:
        m = missing[0]
        why = row[m].reason if m in row and row[m].reason else "no value"
        r.reason = f"{m} not available ({why})"
        return r
    den = row[spec.denominator].value
    assert den is not None
    if den <= 0:
        r.reason = spec.positive_denominator
        return r
    num = spec.combine([row[m].value for m in spec.numerator])  # type: ignore[misc]
    r.value = _round(num / den)
    return r


@dataclass(frozen=True)
class _PriceContext:
    price: float | None
    price_date: date | None
    sec_splits: list[xbrl.SplitEvent]
    provider_splits: list[prices.Split]
    provider_error: str | None


def _splits_after_filing(eps_filed: date, ctx: _PriceContext) -> list[prices.Split]:
    """Provider splits between the EPS filing and the price date not already in SEC data."""
    assert ctx.price_date is not None
    return [
        s
        for s in ctx.provider_splits
        if eps_filed < s.date <= ctx.price_date
        and s.factor not in (0, 1)
        and not any(e.after < s.date <= e.before for e in ctx.sec_splits)
    ]


def _pe(
    row: dict[str, xbrl.FundamentalValue],
    not_applicable: str | None,
    base: dict,
    periods: xbrl.Periods,
    latest: bool,
    ctx: _PriceContext,
) -> RatioValue:
    price, price_date = ctx.price, ctx.price_date
    r = RatioValue(ratio=PE, value=None, formula=PE_FORMULA, source=SOURCE, **base)
    if not_applicable:
        r.reason = not_applicable
        return r
    if periods != "annual":
        r.reason = "P/E uses annual diluted EPS; request periods='annual'"
        return r
    if not latest:
        r.reason = "the price applies to the latest fiscal year only"
        return r
    if price is None or price_date is None:
        r.reason = (
            "no price passed: pass price= and price_date= to compute P/E; no keyless price "
            "source is available"
        )
        return r
    eps = row.get("diluted_eps")
    r.source = (
        f"{SOURCE} (diluted EPS); caller-supplied price {price:g} as of {price_date.isoformat()}"
    )
    if eps is not None:
        r.inputs = {"price": price, "diluted_eps": eps.value}
        for k, val in _sourced([eps]).items():
            setattr(r, k, val)
    if eps is None or eps.value is None:
        why = eps.reason if eps is not None and eps.reason else "no value"
        r.reason = f"diluted_eps not available ({why})"
        return r
    if eps.value <= 0:
        r.reason = "diluted EPS is zero or negative; P/E not meaningful"
        return r
    eps_value = eps.value
    if eps.filed is not None and price_date > eps.filed:
        if ctx.provider_error:
            r.reason = (
                f"cannot confirm the price and EPS share basis match: split check since the "
                f"EPS filing ({eps.filed}) failed ({ctx.provider_error})"
            )
            return r
        later = _splits_after_filing(eps.filed, ctx)
        factor = 1.0
        for s in later:
            factor *= s.factor
        if later:
            eps_value = round(eps_value / factor, 6)
            r.inputs["diluted_eps"] = eps_value
            done = ", ".join(f"{xbrl._ratio_label(s.factor)} on {s.date}" for s in later)
            note = (
                f"EPS divided by {factor:g} for split(s) after its filing ({done}; from the "
                "price provider) to match the price's share basis"
            )
            r.note = f"{r.note}; {note}" if r.note else note
    r.value = _round(price / eps_value)
    return r


# ---------------------------------------------------------------- tool


async def _sic(cik: str) -> tuple[str | None, bool]:
    """(SIC code, stale) from SEC submissions; (None, False) when unavailable."""
    try:
        sub = await edgar.get_submissions(cik)
    except ToolError as exc:
        logger.warning("SIC lookup failed for CIK %s: %s", cik, type(exc).__name__)
        return None, False
    sic = sub.data.get("sic")
    return (str(sic) if sic not in (None, "") else None), sub.stale


# Split history from the configured price provider (needs its key); patched in tests.
split_lookup = prices.get_split_history


async def _price_splits(
    ticker: str, facts: list[xbrl.Fact], cik: str, splits: list[xbrl.SplitEvent], price_date: date
) -> tuple[list[prices.Split], str | None]:
    """Provider splits after the latest annual EPS filing, or the reason the lookup failed."""
    eps = xbrl.normalize(facts, cik=cik, metrics=["diluted_eps"], n=1, splits=splits)
    filed = eps[0].filed if eps else None
    if filed is None or price_date <= filed:
        return [], None
    try:
        return await split_lookup(ticker, filed + timedelta(days=1), price_date), None
    except Exception as exc:  # any provider failure (missing key, HTTP, import) -> null P/E
        logger.warning("split lookup failed for %s: %s", ticker, type(exc).__name__)
        return [], f"price provider unavailable: {type(exc).__name__}"


async def get_key_ratios(
    ticker: str,
    periods: xbrl.Periods = "annual",
    n: int = 4,
    price: float | None = None,
    price_date: date | None = None,
) -> KeyRatios:
    """Key ratios from SEC XBRL filings, newest period first.

    Ratios: gross_margin, operating_margin, net_margin, roe, roa, debt_to_equity,
    current_ratio, fcf_margin ((operating cash flow - capex) / revenue) and pe. periods:
    "annual" or "quarterly" (quarterly uses derived Q4 / year-to-date quarters where
    needed). n: number of periods (1-20). P/E needs a share price: pass price and
    price_date (both required together); it uses the latest fiscal year's diluted EPS, put
    on the price's share basis using splits since the EPS filing. Each ratio carries its
    formula, inputs, source filing URL and filing date. Ratios that do not apply to banks,
    insurers or REITs, or whose inputs are missing, are null with a reason. Descriptive
    figures only. Source: SEC EDGAR XBRL companyfacts.
    """
    if periods not in ("annual", "quarterly"):
        raise BadInput('periods must be "annual" or "quarterly"')
    if not isinstance(n, int) or n < 1 or n > MAX_N:
        raise BadInput(f"n must be between 1 and {MAX_N}")
    if price is not None and not price > 0:
        raise BadInput("price must be a positive number")
    if price is not None and price_date is None:
        raise BadInput("price_date is required with price (every number carries its date)")

    company, url, res, facts = await xbrl.load_company_facts(ticker)
    sic, sub_stale = await _sic(company.cik)
    industry, basis = classify_industry(sic, facts)
    splits = xbrl.detect_splits(facts)
    price_splits: list[prices.Split] = []
    price_split_error = None
    if price is not None and price_date is not None and industry != "reit":
        price_splits, price_split_error = await _price_splits(
            company.ticker, facts, company.cik, splits, price_date
        )
    rows = compute(
        facts,
        cik=company.cik,
        industry=industry,
        periods=periods,
        n=n,
        price=price,
        price_date=price_date,
        splits=splits,
        price_splits=price_splits,
        price_split_error=price_split_error,
    )
    dates = [r.as_of for r in rows if r.as_of is not None]
    return KeyRatios(
        source=SOURCE,
        source_url=url,
        retrieved_at=res.fetched_at,
        as_of=max(dates, default=None),
        stale=res.stale or sub_stale,
        notes=list(NOTES),
        ticker=company.ticker,
        cik=company.cik,
        name=str(res.data.get("entityName") or company.name),
        periods=periods,
        industry=industry,
        industry_basis=basis,
        ratios=rows,
        splits=splits,
    )


def register(mcp: FastMCP) -> None:
    mcp.tool()(get_key_ratios)
