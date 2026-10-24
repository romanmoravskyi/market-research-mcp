"""SEC EDGAR XBRL fundamentals (companyfacts), normalized to canonical metrics.

Layers:
- `parse_facts` turns a companyfacts JSON into flat `Fact`s (us-gaap and dei only).
- Pure selection functions operate on fact lists: `dedupe`, `select_annual`,
  `select_quarterly`, `select_instant`. Facts are chosen by period DURATION, never by the
  filing's `fy`/`fp` (those describe the filing, not the period).
- `normalize` maps facts to canonical metrics with ordered tag fallbacks, chosen per period.
- `get_fundamentals` (the MCP tool) fetches companyfacts and calls `normalize`.

Extension points for derived quarters (Q4 = FY - Q1..Q3, quarterly cash flow from YTD):
add them to the quarterly branch of `duration_facts` and mark the new facts `derived=True`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from market_research_mcp import http
from market_research_mcp.errors import BadInput, NotFound
from market_research_mcp.models import Sourced
from market_research_mcp.sources import edgar

__all__ = [
    "METRICS",
    "SOURCE",
    "Fact",
    "FundamentalValue",
    "Fundamentals",
    "MetricSpec",
    "companyfacts_url",
    "dedupe",
    "duration_facts",
    "fiscal_year_for",
    "get_fundamentals",
    "normalize",
    "parse_facts",
    "register",
    "select_annual",
    "select_instant",
    "select_quarterly",
]

SOURCE = "SEC EDGAR XBRL companyfacts"
COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts/"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"

ANNUAL_DAYS = (350, 380)
QUARTER_DAYS = (80, 100)
COVER_WINDOW_DAYS = 120  # cover-page share counts are dated weeks after the period end
MAX_N = 40

ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
PERIODIC_FORMS = ANNUAL_FORMS | QUARTERLY_FORMS

NO_TAG = "no matching us-gaap tag"
NO_DEI_TAG = "no matching dei tag"
NO_VALUE = "no value reported for this period"

Periods = Literal["annual", "quarterly"]
Kind = Literal["duration", "instant", "cover"]


# ---------------------------------------------------------------- metric map


@dataclass(frozen=True)
class MetricSpec:
    kind: Kind
    unit: str
    tags: tuple[str, ...] = ()
    # Instant sums tried in order, e.g. (("LongTermDebtNoncurrent", "DebtCurrent"),).
    combos: tuple[tuple[str, ...], ...] = ()


METRICS: dict[str, MetricSpec] = {
    "revenue": MetricSpec(
        "duration",
        "USD",
        (
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "RevenuesNetOfInterestExpense",  # banks
        ),
    ),
    "gross_profit": MetricSpec("duration", "USD", ("GrossProfit",)),
    "operating_income": MetricSpec("duration", "USD", ("OperatingIncomeLoss",)),
    "net_income": MetricSpec("duration", "USD", ("NetIncomeLoss",)),
    "diluted_eps": MetricSpec(
        "duration", "USD/shares", ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted")
    ),
    "basic_eps": MetricSpec(
        "duration", "USD/shares", ("EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted")
    ),
    "operating_cash_flow": MetricSpec(
        "duration",
        "USD",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
    ),
    "capex": MetricSpec(
        "duration",
        "USD",
        ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
    ),
    # total_debt = non-current long-term debt + current debt. DebtCurrent (current maturities
    # plus short-term borrowings) is preferred over LongTermDebtCurrent (current maturities
    # only). Fallback: LongTermDebt, which by us-gaap definition includes current maturities
    # but not other short-term borrowings. Lease liabilities are excluded.
    "total_debt": MetricSpec(
        "instant",
        "USD",
        combos=(
            ("LongTermDebtNoncurrent", "DebtCurrent"),
            ("LongTermDebtNoncurrent", "LongTermDebtCurrent"),
            ("LongTermDebt",),
        ),
    ),
    "cash": MetricSpec("instant", "USD", ("CashAndCashEquivalentsAtCarryingValue", "Cash")),
    "shares_diluted": MetricSpec(
        "duration",
        "shares",
        (
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
        ),
    ),
    "shares_outstanding": MetricSpec(
        "cover", "shares", ("dei:EntityCommonStockSharesOutstanding",)
    ),
}

NOTES = [
    "Periods are selected by duration: annual 350-380 days, quarterly 80-100 days; "
    "year-to-date 10-Q facts are excluded.",
    "fiscal_year is the calendar year in which the period (or its fiscal year) ends; a "
    "52/53-week year ending in the first 7 days of January counts as the prior year. A "
    "company's own label can differ.",
    "Duplicate facts (same tag, unit, start, end) keep the latest filed value, so restated "
    "figures win; accession and filed point to that filing.",
    "total_debt = LongTermDebtNoncurrent + DebtCurrent (or + LongTermDebtCurrent), else "
    "LongTermDebt; leases excluded. shares_outstanding is the cover-page count, dated after "
    "the period end.",
    "Values are as reported in USD (EPS in USD/shares); share counts are not split-adjusted.",
]


# ---------------------------------------------------------------- facts


@dataclass(frozen=True)
class Fact:
    """One XBRL fact. `concept` is the bare us-gaap name, or 'dei:Name' for dei."""

    concept: str
    unit: str
    start: date | None
    end: date
    value: int | float
    accession: str
    form: str
    filed: date
    fy: int | None = None
    fp: str | None = None
    derived: bool = False

    @property
    def days(self) -> int | None:
        return (self.end - self.start).days if self.start else None


def parse_facts(data: dict[str, Any]) -> list[Fact]:
    """Flatten a companyfacts JSON (us-gaap and dei taxonomies)."""
    out: list[Fact] = []
    for taxonomy, concepts in (data.get("facts") or {}).items():
        if taxonomy not in ("us-gaap", "dei"):
            continue
        prefix = "" if taxonomy == "us-gaap" else f"{taxonomy}:"
        for name, body in concepts.items():
            for unit, rows in (body.get("units") or {}).items():
                for row in rows:
                    try:
                        out.append(
                            Fact(
                                concept=prefix + name,
                                unit=unit,
                                start=date.fromisoformat(row["start"])
                                if row.get("start")
                                else None,
                                end=date.fromisoformat(row["end"]),
                                value=row["val"],
                                accession=row["accn"],
                                form=str(row.get("form") or ""),
                                filed=date.fromisoformat(row["filed"]),
                                fy=row.get("fy"),
                                fp=row.get("fp"),
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
    return out


def dedupe(facts: Iterable[Fact]) -> list[Fact]:
    """Keep one fact per (concept, unit, start, end): the latest `filed` (ties: accession)."""
    best: dict[tuple[str, str, date | None, date], Fact] = {}
    for f in facts:
        key = (f.concept, f.unit, f.start, f.end)
        cur = best.get(key)
        if cur is None or (f.filed, f.accession) > (cur.filed, cur.accession):
            best[key] = f
    return list(best.values())


def _periodic(facts: Iterable[Fact], forms: frozenset[str]) -> list[Fact]:
    return dedupe(f for f in facts if f.form.upper() in forms)


def select_annual(facts: Iterable[Fact]) -> list[Fact]:
    """Duration facts 350-380 days long from 10-K filings, deduped."""
    lo, hi = ANNUAL_DAYS
    return [f for f in _periodic(facts, ANNUAL_FORMS) if f.days is not None and lo <= f.days <= hi]


def select_quarterly(facts: Iterable[Fact]) -> list[Fact]:
    """Duration facts genuinely 80-100 days long from 10-Q/10-K filings, deduped.

    Year-to-date 10-Q facts (about 180 or 270 days) are dropped here.
    """
    lo, hi = QUARTER_DAYS
    return [
        f for f in _periodic(facts, PERIODIC_FORMS) if f.days is not None and lo <= f.days <= hi
    ]


def duration_facts(facts: Iterable[Fact], periods: Periods) -> list[Fact]:
    """Flow facts for the requested periodicity. Derived quarters will plug in here."""
    facts = list(facts)
    return select_annual(facts) if periods == "annual" else select_quarterly(facts)


def select_instant(facts: Iterable[Fact], ends: Iterable[date]) -> list[Fact]:
    """Balance-sheet (instant) facts whose `end` equals one of the fiscal period ends."""
    wanted = set(ends)
    return [f for f in _periodic(facts, PERIODIC_FORMS) if f.start is None and f.end in wanted]


def fiscal_year_for(end: date) -> int:
    """Fiscal year from a fiscal-year end date: its calendar year, except a 52/53-week year
    ending in the first 7 days of January, which belongs to the prior year."""
    return end.year - 1 if end.month == 1 and end.day <= 7 else end.year


def _fiscal_label(end: date, periods: Periods, fy_ends: list[date]) -> tuple[int, str | None]:
    if periods == "annual":
        return fiscal_year_for(end), "FY"
    fy_end = next((e for e in fy_ends if 0 <= (e - end).days < 370), None)
    if fy_end is None and fy_ends:
        fy_end = fy_ends[-1]
        while fy_end < end - timedelta(days=5):
            fy_end = _add_year(fy_end)
    if fy_end is None:
        return fiscal_year_for(end), None
    quarter = 4 - round((fy_end - end).days / 91.31)
    return fiscal_year_for(fy_end), f"Q{min(4, max(1, quarter))}"


def _add_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # Feb 29
        return d.replace(year=d.year + 1, day=28)


# ---------------------------------------------------------------- output models


class FundamentalValue(BaseModel):
    metric: str
    value: int | float | None
    unit: str
    period_start: date | None = None
    period_end: date
    fiscal_year: int
    fiscal_period: str | None = None
    tag: str | None = None
    form: str | None = None
    accession: str | None = None
    filed: date | None = None
    derived: bool = False
    source_url: str | None = None
    reason: str | None = None


class Fundamentals(Sourced):
    ticker: str
    cik: str
    name: str
    periods: Periods
    metrics: list[str]
    values: list[FundamentalValue]


# ---------------------------------------------------------------- normalization


def companyfacts_url(cik: int | str) -> str:
    return f"{COMPANYFACTS_BASE}CIK{edgar.pad_cik(cik)}.json"


def filing_index_url(cik: int | str, accession: str) -> str:
    return f"{ARCHIVES_BASE}{int(str(cik))}/{accession.replace('-', '')}/"


def _anchor_tags() -> set[str]:
    return {t for s in METRICS.values() if s.kind == "duration" for t in s.tags}


def _value(metric: str, spec: MetricSpec, cik: str, f: Fact, tag: str, **kw: Any):
    return FundamentalValue(
        metric=metric,
        value=f.value,
        unit=spec.unit,
        tag=tag,
        form=f.form,
        accession=f.accession,
        filed=f.filed,
        derived=f.derived,
        source_url=filing_index_url(cik, f.accession),
        **kw,
    )


def normalize(
    facts: list[Fact],
    *,
    cik: str,
    metrics: list[str] | None = None,
    periods: Periods = "annual",
    n: int = 5,
) -> list[FundamentalValue]:
    """Map facts to canonical metrics for the newest `n` periods (newest first).

    Fallback tags are tried per period: the first tag with a value for that period wins.
    Metrics with no matching tag at all, or no value for a period, return value None with a
    reason. Nothing is estimated or filled in.
    """
    names = metrics or list(METRICS)
    present = {(f.concept, f.unit) for f in facts}
    flows = duration_facts(facts, periods)
    anchors = _anchor_tags()

    # Period list: every (start, end) of a flow fact for any canonical tag, keyed by end.
    starts: dict[date, date | None] = {}
    for f in flows:
        if f.concept in anchors:
            starts.setdefault(f.end, f.start)
    ends = sorted(starts, reverse=True)[:n]
    fy_ends = sorted({f.end for f in select_annual(facts) if f.concept in anchors})

    by_key: dict[tuple[str, str, date], Fact] = {}
    for f in flows:  # same end, different start (rare): prefer the period's start
        key = (f.concept, f.unit, f.end)
        cur = by_key.get(key)
        if cur is None or (cur.start != starts.get(f.end) and f.start == starts.get(f.end)):
            by_key[key] = f
    instants = {(f.concept, f.unit, f.end): f for f in select_instant(facts, ends)}
    covers = sorted(
        (f for f in dedupe(facts) if f.concept.startswith("dei:") and f.start is None),
        key=lambda f: f.end,
    )

    out: list[FundamentalValue] = []
    for end in ends:
        fiscal_year, fiscal_period = _fiscal_label(end, periods, fy_ends)
        label = {"fiscal_year": fiscal_year, "fiscal_period": fiscal_period}
        for metric in names:
            spec = METRICS[metric]
            tags = spec.tags or tuple(t for combo in spec.combos for t in combo)
            null = FundamentalValue(
                metric=metric, value=None, unit=spec.unit, period_end=end, **label
            )
            if not any((t, spec.unit) in present for t in tags):
                null.reason = NO_DEI_TAG if spec.kind == "cover" else NO_TAG
                out.append(null)
                continue
            found: FundamentalValue | None = None
            if spec.kind == "duration":
                for tag in spec.tags:
                    f = by_key.get((tag, spec.unit, end))
                    if f is not None:
                        kw = {"period_start": f.start, "period_end": end, **label}
                        found = _value(metric, spec, cik, f, tag, **kw)
                        break
            elif spec.kind == "instant":
                found = _instant(metric, spec, cik, instants, end, label)
            else:
                found = _cover(metric, spec, cik, covers, end, label)
            if found is None:
                null.reason = NO_VALUE
                found = null
            out.append(found)
    return out


def _instant(
    metric: str,
    spec: MetricSpec,
    cik: str,
    instants: dict[tuple[str, str, date], Fact],
    end: date,
    label: dict[str, Any],
) -> FundamentalValue | None:
    combos = spec.combos or tuple((t,) for t in spec.tags)
    for combo in combos:
        parts = [instants.get((t, spec.unit, end)) for t in combo]
        if all(p is not None for p in parts):
            facts = [p for p in parts if p is not None]
            latest = max(facts, key=lambda p: (p.filed, p.accession))
            value = sum(p.value for p in facts)
            v = _value(metric, spec, cik, latest, "+".join(combo), period_end=end, **label)
            v.value = value
            return v
    return None


def _cover(
    metric: str,
    spec: MetricSpec,
    cik: str,
    covers: list[Fact],
    end: date,
    label: dict[str, Any],
) -> FundamentalValue | None:
    """Earliest cover-page value dated on or up to COVER_WINDOW_DAYS after the period end."""
    limit = end + timedelta(days=COVER_WINDOW_DAYS)
    for tag in spec.tags:
        for f in covers:
            if f.concept == tag and f.unit == spec.unit and end <= f.end <= limit:
                return _value(metric, spec, cik, f, tag, period_end=f.end, **label)
    return None


# ---------------------------------------------------------------- tool


async def get_fundamentals(
    ticker: str,
    metrics: list[str] | None = None,
    periods: Periods = "annual",
    n: int = 5,
) -> Fundamentals:
    """Company fundamentals from SEC XBRL filings (10-K/10-Q), newest period first.

    metrics: any of revenue, gross_profit, operating_income, net_income, diluted_eps,
    basic_eps, operating_cash_flow, capex, total_debt, cash, shares_diluted,
    shares_outstanding (default: all). periods: "annual" or "quarterly". n: number of
    periods (1-40). Each value carries the XBRL tag used, the period dates, the filing's
    form, accession, filed date and URL. Missing values are null with a reason.
    Source: SEC EDGAR XBRL companyfacts.
    """
    if periods not in ("annual", "quarterly"):
        raise BadInput('periods must be "annual" or "quarterly"')
    if not isinstance(n, int) or n < 1 or n > MAX_N:
        raise BadInput(f"n must be between 1 and {MAX_N}")
    names = list(dict.fromkeys(m.strip().lower() for m in metrics or [] if m.strip()))
    unknown = [m for m in names if m not in METRICS]
    if unknown:
        raise BadInput(f"unknown metrics {unknown}; valid metrics: {', '.join(METRICS)}")
    names = names or list(METRICS)

    company = await edgar.get_company(ticker)
    url = companyfacts_url(company.cik)
    res = await http.fetch("edgar", url, cache_ns="companyfacts", cache_key=f"CIK{company.cik}")
    facts = parse_facts(res.data)
    if not facts:
        raise NotFound(f"XBRL facts for {company.ticker}", SOURCE)
    values = normalize(facts, cik=company.cik, metrics=names, periods=periods, n=n)
    filed = [v.filed for v in values if v.filed is not None]
    notes = list(NOTES)
    if periods == "quarterly":
        notes.append(
            "Quarterly values are only facts reported for a 3-month period; fiscal Q4 and "
            "quarterly cash flow (reported year-to-date) are not derived yet."
        )
    return Fundamentals(
        source=SOURCE,
        source_url=url,
        retrieved_at=res.fetched_at,
        as_of=max(filed, default=None),
        stale=res.stale,
        notes=notes,
        ticker=company.ticker,
        cik=company.cik,
        name=str(res.data.get("entityName") or company.name),
        periods=periods,
        metrics=names,
        values=values,
    )


def register(mcp: FastMCP) -> None:
    mcp.tool()(get_fundamentals)
