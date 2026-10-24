"""SEC EDGAR XBRL fundamentals (companyfacts), normalized to canonical metrics.

Layers:
- `parse_facts` turns a companyfacts JSON into flat `Fact`s (us-gaap and dei only).
- Pure selection functions operate on fact lists: `dedupe`, `select_annual`,
  `select_quarterly`, `select_instant`. Facts are chosen by period DURATION, never by the
  filing's `fy`/`fp` (those describe the filing, not the period).
- `normalize` maps facts to canonical metrics with ordered tag fallbacks, chosen per period.
- `get_fundamentals` (the MCP tool) fetches companyfacts and calls `normalize`.

Derived quarters (`derive_quarters`): fiscal Q4 (FY - 9M YTD, or FY - Q1 - Q2 - Q3) and
discrete quarters from year-to-date 10-Q facts (Q2 = 6M - 3M, Q3 = 9M - 6M), for additive
USD flow metrics only. Derived facts carry `derived=True` and a note naming the formula and
the filings used.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from fractions import Fraction
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
    "derive_quarters",
    "duration_facts",
    "fiscal_year_for",
    "get_fundamentals",
    "load_company_facts",
    "normalize",
    "parse_facts",
    "register",
    "select_annual",
    "select_instant",
    "select_quarterly",
    "SplitEvent",
    "detect_splits",
]

SOURCE = "SEC EDGAR XBRL companyfacts"
COMPANYFACTS_BASE = "https://data.sec.gov/api/xbrl/companyfacts/"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"

ANNUAL_DAYS = (350, 380)
QUARTER_DAYS = (80, 100)
HALF_YTD_DAYS = (170, 195)
NINE_MONTH_YTD_DAYS = (260, 290)
COVER_WINDOW_DAYS = 120  # cover-page share counts are dated weeks after the period end
MAX_N = 40

ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})
PERIODIC_FORMS = ANNUAL_FORMS | QUARTERLY_FORMS

NO_TAG = "no matching us-gaap tag"
NO_DEI_TAG = "no matching dei tag"
NO_VALUE = "no value reported for this period"
NOT_ADDITIVE = (
    "not reported for this quarter; per-share and average share values are not additive, "
    "so they are not derived from year-to-date figures"
)

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
    # Fallback for filers that tag only net income available to common stockholders (after
    # preferred dividends); the tag shown per value says which one was used.
    "net_income": MetricSpec(
        "duration", "USD", ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic")
    ),
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
    # Equity attributable to the parent first (matches NetIncomeLoss), then including
    # noncontrolling interests.
    "stockholders_equity": MetricSpec(
        "instant",
        "USD",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ),
    ),
    "total_assets": MetricSpec("instant", "USD", ("Assets",)),
    "current_assets": MetricSpec("instant", "USD", ("AssetsCurrent",)),
    "current_liabilities": MetricSpec("instant", "USD", ("LiabilitiesCurrent",)),
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
    "Periods are selected by duration: annual 350-380 days, quarterly 80-100 days. "
    "Year-to-date 10-Q facts are never shown as quarters; they only feed derived quarters "
    "(quarterly periods, derived=true).",
    "fiscal_year is the calendar year in which the period (or its fiscal year) ends; a "
    "52/53-week year ending in the first 7 days of January counts as the prior year. A "
    "company's own label can differ.",
    "Duplicate facts (same tag, unit, start, end) keep the latest filed value, so restated "
    "figures win; accession and filed point to that filing.",
    "total_debt = LongTermDebtNoncurrent + DebtCurrent (or + LongTermDebtCurrent), else "
    "LongTermDebt; leases excluded. shares_outstanding is the cover-page count, dated after "
    "the period end.",
    "Values are as reported in USD (EPS in USD/shares). Share counts and per-share values are "
    "split-adjusted: the company's own restated figures are preferred; values filed only "
    "before a split are multiplied (shares) or divided (per share) by the split ratio "
    "(split_factor, with a note). Splits are detected from SEC facts restated by a clean "
    "ratio between consecutive filings. Mechanically adjusted EPS keeps more decimals than "
    "company-restated EPS, which is rounded to cents.",
]


# ---------------------------------------------------------------- facts


@dataclass(frozen=True)
class Fact:
    """One XBRL fact. `concept` is the bare us-gaap name, or 'dei:Name' for dei."""

    concept: str
    unit: str
    start: date | None
    end: date
    value: int | float | None  # None only for a rejected derived quarter (see note)
    accession: str
    form: str
    filed: date
    fy: int | None = None
    fp: str | None = None
    derived: bool = False
    note: str | None = None

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


def _in(days: int | None, band: tuple[int, int]) -> bool:
    return days is not None and band[0] <= days <= band[1]


def _cumulative_index(days: int | None) -> int | None:
    """1, 2, 3 or 4 for a 3-, 6-, 9- or 12-month period from the fiscal-year start."""
    for k, band in enumerate((QUARTER_DAYS, HALF_YTD_DAYS, NINE_MONTH_YTD_DAYS, ANNUAL_DAYS), 1):
        if _in(days, band):
            return k
    return None


_CUM_NAME = {1: "3M", 2: "6M YTD", 3: "9M YTD", 4: "FY"}
DERIVED_BAND = (0.2, 5.0)  # a derived revenue quarter vs the mean of the year's other quarters


def _implausible(concept: str, value: float, others: list[Fact]) -> str | None:
    """Why a derived quarter cannot be right, for metrics that are never negative."""
    if concept in _non_negative_tags() and value < 0:
        return "negative"
    if concept in _revenue_tags():
        if value <= 0:
            return "zero or negative revenue"
        known = [f.value for f in others if f.value is not None and f.value > 0]
        if known:
            mean = sum(known) / len(known)
            lo, hi = DERIVED_BAND
            if not lo * mean <= value <= hi * mean:
                return f"outside {lo:g}x-{hi:g}x of the year's other quarters"
    return None


def _derived_fact(template: Fact, start: date, end: date, terms: list[tuple[str, Fact]]) -> Fact:
    """A derived quarter: first term minus the others. Sourced to the latest filing used."""
    value = terms[0][1].value - sum(f.value for _, f in terms[1:])  # type: ignore[operator]
    if isinstance(value, float):
        value = round(value, 6)
    latest = max((f for _, f in terms), key=lambda f: (f.filed, f.accession))
    formula = " - ".join(name for name, _ in terms)
    used = "; ".join(
        f"{name} from {f.form} {f.accession}" + (" (derived)" if f.derived else "")
        for name, f in terms
    )
    return Fact(
        concept=template.concept,
        unit=template.unit,
        start=start,
        end=end,
        value=value,
        accession=latest.accession,
        form=latest.form,
        filed=latest.filed,
        derived=True,
        note=f"derived: {formula}; {used}",
    )


def derive_quarters(facts: Iterable[Fact], concepts: Iterable[str]) -> list[Fact]:
    """Discrete quarters missing as 3-month facts, derived from cumulative facts.

    For each fiscal year (cumulative facts sharing a start date) the quarters are walked in
    order. A quarter with a reported 3-month fact is kept as is. A missing quarter k is
    derived as cumulative(k) - cumulative(k-1) (Q2 = 6M YTD - 3M, Q3 = 9M YTD - 6M YTD,
    Q4 = FY - 9M YTD), else as cumulative(k) minus the earlier discrete quarters
    (Q4 = FY - Q1 - Q2 - Q3). Only use for additive metrics (USD flows), never per-share or
    average share counts. Returns only the derived facts.
    """
    wanted = set(concepts)
    periodic = [f for f in _periodic(facts, PERIODIC_FORMS) if f.concept in wanted and f.start]
    direct: dict[tuple[str, str, date], Fact] = {}
    groups: dict[tuple[str, str, date], dict[int, Fact]] = {}
    for f in periodic:
        assert f.start is not None
        if _in(f.days, QUARTER_DAYS):
            direct.setdefault((f.concept, f.unit, f.start), f)
        k = _cumulative_index(f.days)
        if k is not None:
            cum = groups.setdefault((f.concept, f.unit, f.start), {})
            if k not in cum or (f.filed, f.accession) > (cum[k].filed, cum[k].accession):
                cum[k] = f

    out: list[Fact] = []
    for (concept, unit, fy_start), cum in sorted(groups.items(), key=lambda kv: kv[0][2]):
        if len(cum) < 2:
            continue
        q_start = fy_start
        quarters: list[Fact] = []
        for k in range(1, 5):
            q = direct.get((concept, unit, q_start))
            if q is None:
                total = cum.get(k)
                if k == 1 or total is None:
                    break
                prev = cum.get(k - 1)
                if prev is not None and prev.end + timedelta(days=1) == q_start:
                    terms = [(_CUM_NAME[k], total), (_CUM_NAME[k - 1], prev)]
                elif len(quarters) == k - 1:
                    terms = [(_CUM_NAME[k], total)] + [
                        (f"Q{i}", q) for i, q in enumerate(quarters, 1)
                    ]
                else:
                    break
                if not _in((total.end - q_start).days, QUARTER_DAYS):
                    break
                q = _derived_fact(total, q_start, total.end, terms)
                assert q.value is not None
                why = _implausible(concept, q.value, quarters)
                if why:
                    # Usually a recast between filings (e.g. discontinued operations).
                    note = f"implausible derived value {q.value:g} ({why}); {q.note}"
                    out.append(replace(q, value=None, note=note))
                    break
                direct[(concept, unit, q_start)] = q
                out.append(q)
            quarters.append(q)
            q_start = q.end + timedelta(days=1)
    return out


def _revenue_tags() -> set[str]:
    return set(METRICS["revenue"].tags) | set(METRICS["gross_profit"].tags)


def _non_negative_tags() -> set[str]:
    return _revenue_tags() | set(METRICS["capex"].tags)


def _additive_tags() -> set[str]:
    return {t for s in METRICS.values() if s.kind == "duration" and s.unit == "USD" for t in s.tags}


def duration_facts(facts: Iterable[Fact], periods: Periods) -> list[Fact]:
    """Flow facts for the requested periodicity; quarterly adds derived quarters."""
    facts = list(facts)
    if periods == "annual":
        return select_annual(facts)
    return select_quarterly(facts) + derive_quarters(facts, _additive_tags())


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
    if not fy_ends:
        return fiscal_year_for(end), None
    fy_end = next((e for e in fy_ends if 0 <= (e - end).days < 370), None)
    if fy_end is None:
        # Outside the known annual periods (or a gap): extrapolate the fiscal-year-end date
        # in whole years from the nearest known one until it is the first on or after `end`.
        floor = end - timedelta(days=5)
        fy_end = next((e for e in sorted(fy_ends) if e >= floor), max(fy_ends))
        while _add_year(fy_end, -1) >= floor:
            fy_end = _add_year(fy_end, -1)
        while fy_end < floor:
            fy_end = _add_year(fy_end)
    quarter = 4 - round((fy_end - end).days / 91.31)
    return fiscal_year_for(fy_end), f"Q{min(4, max(1, quarter))}"


def _add_year(d: date, years: int = 1) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # Feb 29
        return d.replace(year=d.year + years, day=28)


# ---------------------------------------------------------------- splits

SPLIT_MIN_RATIO = 1.2  # smaller changes are ordinary restatements, not splits
SPLIT_MAX_RATIO = 500  # 1000x and up are unit-scale fixes (thousands vs units), not splits
SPLIT_TOLERANCE = 0.005
SPLIT_MIN_EVIDENCE = 2  # restated (period, concept) pairs needed to accept a split
SPLIT_UNITS = frozenset({"shares"})
PER_SHARE_UNIT = "USD/shares"


class SplitEvent(BaseModel):
    """A stock split. `ratio` = new shares per old share (10 for 10:1, 0.1 for 1:10).

    The split took effect after the filing dated `after` and on or before the filing dated
    `before`. Facts filed before `before` are on the old basis.
    """

    ratio: float
    after: date
    before: date
    evidence: int


def _snap_ratio(r: float) -> float | None:
    if r <= 0:
        return None
    big = max(r, 1 / r)
    if not SPLIT_MIN_RATIO <= big <= SPLIT_MAX_RATIO:
        return None
    frac = Fraction(big).limit_denominator(4)
    if frac == 0 or abs(big - float(frac)) / float(frac) > SPLIT_TOLERANCE:
        return None
    ratio = float(frac)
    return ratio if r >= 1 else 1 / ratio


def detect_splits(facts: Iterable[Fact]) -> list[SplitEvent]:
    """Stock splits implied by share-count facts restated between consecutive filings.

    For every (concept, period) reported in more than one 10-K/10-Q, consecutive filings
    whose values differ by a clean ratio (2, 3:2, 10, 1:10, ...) mark a split between those
    filing dates. Pairs with the same ratio and overlapping windows are merged; a split
    needs at least SPLIT_MIN_EVIDENCE pairs. Oldest first.
    """
    series: dict[tuple[str, str, date | None, date], list[Fact]] = {}
    for f in facts:
        if f.unit in SPLIT_UNITS and f.form.upper() in PERIODIC_FORMS and f.value:
            series.setdefault((f.concept, f.unit, f.start, f.end), []).append(f)
    pairs: list[tuple[float, date, date]] = []
    for rows in series.values():
        by_filed: dict[date, Fact] = {}
        for f in rows:
            by_filed.setdefault(f.filed, f)
        ordered = [by_filed[d] for d in sorted(by_filed)]
        for old, new in zip(ordered, ordered[1:], strict=False):
            ratio = _snap_ratio(new.value / old.value)
            if ratio is not None:
                pairs.append((ratio, old.filed, new.filed))
    clusters: list[list[Any]] = []  # [ratio, after, before, evidence]
    for ratio, old_filed, new_filed in sorted(pairs, key=lambda p: (p[0], p[2], p[1])):
        for c in clusters:
            lo, hi = max(c[1], old_filed), min(c[2], new_filed)
            if c[0] == ratio and lo < hi:
                c[1], c[2], c[3] = lo, hi, c[3] + 1
                break
        else:
            clusters.append([ratio, old_filed, new_filed, 1])
    events = [
        SplitEvent(ratio=r, after=a, before=b, evidence=e)
        for r, a, b, e in clusters
        if e >= SPLIT_MIN_EVIDENCE
    ]
    return sorted((e for e in events if not _composite(e, events)), key=lambda s: s.before)


def _composite(event: SplitEvent, events: list[SplitEvent]) -> bool:
    """True when `event` is two or more other splits seen across one wide filing gap."""
    inner = [
        o for o in events if o is not event and event.after <= o.after and o.before <= event.before
    ]
    if len(inner) < 2:
        return False
    product = 1.0
    for o in inner:
        product *= o.ratio
    return abs(product - event.ratio) / event.ratio <= SPLIT_TOLERANCE


def _ratio_label(ratio: float) -> str:
    """'10:1' for a forward split, '1:10' for a reverse split (ratio 0.1)."""
    frac = Fraction(max(ratio, 1 / ratio)).limit_denominator(4)
    a, b = frac.numerator, frac.denominator
    return f"{a}:{b}" if ratio >= 1 else f"{b}:{a}"


def _split_adjust(v: FundamentalValue, splits: list[SplitEvent]) -> FundamentalValue:
    """Put a share count or per-share value filed before a split on the current basis."""
    if v.value is None or v.filed is None or v.unit not in SPLIT_UNITS | {PER_SHARE_UNIT}:
        return v
    applied = [s for s in splits if v.filed < s.before]
    if not applied:
        return v
    factor = 1.0
    for s in applied:
        factor *= s.ratio
    if v.unit == PER_SHARE_UNIT:
        v.value = round(v.value / factor, 6)
    else:
        v.value = round(v.value * factor)
    v.split_factor = factor
    what = "; ".join(
        f"{_ratio_label(s.ratio)} split detected from SEC restatements between filings of "
        f"{s.after} and {s.before}"
        for s in applied
    )
    op = "divided" if v.unit == PER_SHARE_UNIT else "multiplied"
    note = f"split-adjusted: value filed {v.filed} {op} by {factor:g} ({what})"
    v.note = f"{v.note}; {note}" if v.note else note
    return v


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
    split_factor: float = 1.0
    note: str | None = None
    source_url: str | None = None
    reason: str | None = None


class Fundamentals(Sourced):
    ticker: str
    cik: str
    name: str
    periods: Periods
    metrics: list[str]
    values: list[FundamentalValue]
    splits: list[SplitEvent] = []


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
        note=f.note,
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
    splits: list[SplitEvent] | None = None,
) -> list[FundamentalValue]:
    """Map facts to canonical metrics for the newest `n` periods (newest first).

    Share counts and per-share values filed before a split are adjusted to the current
    basis; `splits` defaults to `detect_splits(facts)`.

    Fallback tags are tried per period: the first tag with a value for that period wins.
    Metrics with no matching tag at all, or no value for a period, return value None with a
    reason. Nothing is estimated; quarterly flows missing as 3-month facts are derived by
    subtraction (see `derive_quarters`) and marked derived. A reported 3-month fact under any
    fallback tag wins over a derived one.
    """
    names = metrics or list(METRICS)
    if splits is None:
        splits = detect_splits(facts)
    present = {(f.concept, f.unit) for f in facts}
    flows = duration_facts(facts, periods)
    anchors = _anchor_tags()

    # Period list: every (start, end) of a flow fact for any canonical tag, keyed by end.
    starts: dict[date, date | None] = {}
    for f in flows:
        if f.concept in anchors:
            starts.setdefault(f.end, f.start)
    ends = sorted(starts, reverse=True)[:n]
    reported_ends = {f.end for f in flows if f.concept in anchors and not f.derived}
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
                candidates = [
                    (f.derived, i, tag, f)
                    for i, tag in enumerate(spec.tags)
                    if (f := by_key.get((tag, spec.unit, end))) is not None
                ]
                if candidates:
                    _, _, tag, f = min(candidates, key=lambda c: (c[0], c[1]))
                    kw = {"period_start": f.start, "period_end": end, **label}
                    found = _value(metric, spec, cik, f, tag, **kw)
                    if f.value is None:
                        found.reason = f.note
            elif spec.kind == "instant":
                found = _instant(metric, spec, cik, instants, end, label)
            else:
                found = _cover(metric, spec, cik, covers, end, label)
            if found is None:
                additive = spec.kind != "duration" or spec.unit == "USD"
                null.reason = NO_VALUE if additive or end in reported_ends else NOT_ADDITIVE
                found = null
            out.append(_split_adjust(found, splits))
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


# Fallback tags that measure something different from the metric's primary tag.
TAG_CAVEATS = {
    "NetIncomeLossAvailableToCommonStockholdersBasic": (
        "net income available to common stockholders, after preferred dividends; not the "
        "same measure as NetIncomeLoss"
    ),
}


def mixed_tag_notes(values: list[FundamentalValue]) -> list[str]:
    """One note per metric whose series uses more than one tag (tags differ by period)."""
    notes: list[str] = []
    by_metric: dict[str, dict[str, list[date]]] = {}
    for v in values:
        if v.tag and v.value is not None:
            by_metric.setdefault(v.metric, {}).setdefault(v.tag, []).append(v.period_end)
    for metric, tags in by_metric.items():
        if len(tags) < 2:
            continue
        parts = []
        for tag, ends in tags.items():
            caveat = f": {TAG_CAVEATS[tag]}" if tag in TAG_CAVEATS else ""
            parts.append(f"{tag} for periods ending {min(ends)} to {max(ends)}{caveat}")
        notes.append(
            f"{metric} mixes XBRL tags across periods: {'; '.join(parts)}. Each value's `tag` "
            "says which; compare values only within one tag. Derived quarters never mix tags."
        )
    return notes


# ---------------------------------------------------------------- tool


async def load_company_facts(ticker: str) -> tuple[edgar.CompanyRef, str, Any, list[Fact]]:
    """Resolve `ticker` and fetch its companyfacts: (company, url, fetch result, facts)."""
    company = await edgar.get_company(ticker)
    url = companyfacts_url(company.cik)
    res = await http.fetch("edgar", url, cache_ns="companyfacts", cache_key=f"CIK{company.cik}")
    facts = parse_facts(res.data)
    if not facts:
        raise NotFound(f"XBRL facts for {company.ticker}", SOURCE)
    return company, url, res, facts


async def get_fundamentals(
    ticker: str,
    metrics: list[str] | None = None,
    periods: Periods = "annual",
    n: int = 5,
) -> Fundamentals:
    """Company fundamentals from SEC XBRL filings (10-K/10-Q), newest period first.

    metrics: any of revenue, gross_profit, operating_income, net_income, diluted_eps,
    basic_eps, operating_cash_flow, capex, total_debt, cash, stockholders_equity,
    total_assets, current_assets, current_liabilities, shares_diluted, shares_outstanding
    (default: all). periods: "annual" or "quarterly". n: number of
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

    company, url, res, facts = await load_company_facts(ticker)
    splits = detect_splits(facts)
    values = normalize(facts, cik=company.cik, metrics=names, periods=periods, n=n, splits=splits)
    filed = [v.filed for v in values if v.filed is not None]
    notes = list(NOTES) + mixed_tag_notes(values)
    if periods == "quarterly":
        notes.append(
            "Quarterly values are 3-month facts where reported. Missing quarters of USD flow "
            "metrics are derived (derived=true, formula in note): Q4 = FY - 9M YTD (or FY - "
            "Q1 - Q2 - Q3); year-to-date cash flow gives Q2 = 6M - 3M and Q3 = 9M - 6M. EPS "
            "and share counts are never derived."
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
        splits=splits,
    )


def register(mcp: FastMCP) -> None:
    mcp.tool()(get_fundamentals)
