"""Earnings calendar: Finnhub first, SEC EDGAR estimate as a per-ticker fallback.

Read-only. Forecast fields (EPS and revenue estimates) are never returned. Each row states
exactly one method: `finnhub`, `edgar_estimate` or `none`, with its own source and URL.
The Finnhub key travels only in the `X-Finnhub-Token` header, never in a URL or result.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from mcp.server.fastmcp import FastMCP

from market_research_mcp import http
from market_research_mcp.config import ConfigError, require
from market_research_mcp.errors import BadInput, ToolError
from market_research_mcp.models import Sourced, utcnow
from market_research_mcp.sources import edgar
from market_research_mcp.tickers import normalize_ticker

__all__ = [
    "EDGAR_SOURCE",
    "FINNHUB_DOCS_URL",
    "EarningsCalendar",
    "EarningsRow",
    "Estimate",
    "estimate_next",
    "get_earnings_calendar",
    "register",
]

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
FINNHUB_DOCS_URL = "https://finnhub.io/docs/api/earnings-calendar"
FINNHUB_SOURCE = "Finnhub"
EDGAR_SOURCE = "SEC EDGAR (estimated from prior 8-K Item 2.02 dates)"
MAX_TICKERS = 20
MAX_DAYS_AHEAD = 180
HOURS = {"bmo", "amc", "dmh"}
TYPICAL_QUARTER_DAYS = 91
CADENCE_FILINGS = 5  # up to 4 gaps between the latest Item 2.02 8-Ks
NO_CONFIRMED_NOTE = (
    "Finnhub does not say whether this date is confirmed by the company; confirmed is unknown."
)

Method = Literal["finnhub", "edgar_estimate", "none"]
Basis = Literal["prior_year_same_quarter", "quarterly_cadence"]


def _today() -> date:
    """Today's date (UTC). Replaced in tests."""
    return utcnow().date()


class EarningsRow(Sourced):
    ticker: str
    method: Method
    next_earnings_date: date | None = None
    hour: str | None = None  # bmo (before open), amc (after close), dmh (during hours)
    fiscal_quarter: int | None = None
    fiscal_year: int | None = None
    confirmed: bool | None = None
    estimated: bool = False
    estimate_basis: Basis | None = None


class EarningsCalendar(Sourced):
    window_start: date
    window_end: date
    rows: list[EarningsRow]


@dataclass(frozen=True)
class Estimate:
    date: date
    basis: Basis
    basis_filing: date


class _Unavailable(Exception):
    """Finnhub gave nothing usable; the message says why (no secrets)."""


def _plus_one_year(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 February
        return d.replace(year=d.year + 1, day=28)


def estimate_next(filing_dates: list[date], today: date, window_end: date) -> Estimate | None:
    """Estimate the next earnings date from past Item 2.02 8-K filing dates.

    First choice: the same quarter last year plus one year (any of the last four filings).
    Second: the latest filing plus the median gap of recent filings (about 91 days).
    Returns None when neither estimate falls inside [today, window_end].
    """
    dates = sorted(set(filing_dates), reverse=True)
    if not dates:
        return None
    anniversaries = sorted((_plus_one_year(d), d) for d in dates[:4] if _plus_one_year(d) >= today)
    if anniversaries and anniversaries[0][0] <= window_end:
        est, basis = anniversaries[0]
        return Estimate(est, "prior_year_same_quarter", basis)

    recent = dates[:CADENCE_FILINGS]
    gaps = [(a - b).days for a, b in zip(recent, recent[1:], strict=False)]
    gaps = [g for g in gaps if 60 <= g <= 120]
    gap = round(statistics.median(gaps)) if gaps else TYPICAL_QUARTER_DAYS
    latest = dates[0]
    est = latest + timedelta(days=gap)
    for _ in range(8):
        if est >= today:
            break
        est += timedelta(days=gap)
    if today <= est <= window_end:
        return Estimate(est, "quarterly_cadence", latest)
    return None


def _normalize(tickers: list[str]) -> list[str]:
    out: list[str] = []
    for t in tickers:
        norm = normalize_ticker(t)
        if norm not in out:
            out.append(norm)
    if not out:
        raise BadInput("tickers must contain at least one symbol")
    if len(out) > MAX_TICKERS:
        raise BadInput(f"at most {MAX_TICKERS} tickers per call, got {len(out)}")
    return out


def _finnhub_symbol(ticker: str) -> str:
    """Finnhub writes US share classes with a dot ('BRK-B' -> 'BRK.B')."""
    return ticker.replace("-", ".")


async def _finnhub_row(ticker: str, start: date, end: date) -> EarningsRow:
    try:
        key = require("FINNHUB_API_KEY")
    except ConfigError:
        raise _Unavailable("FINNHUB_API_KEY is not set") from None
    symbol = _finnhub_symbol(ticker)
    try:
        res = await http.fetch(
            "finnhub",
            FINNHUB_URL,
            params={"from": start.isoformat(), "to": end.isoformat(), "symbol": symbol},
            headers={"X-Finnhub-Token": key},
            cache_ns="calendar",
            cache_key=f"{symbol}:{start.isoformat()}:{end.isoformat()}",
        )
    except ToolError as exc:
        raise _Unavailable(str(exc)) from None
    data = res.data
    if not isinstance(data, dict) or not isinstance(data.get("earningsCalendar"), list):
        raise _Unavailable("Finnhub returned an unexpected response")
    best: tuple[date, dict] | None = None
    for item in data["earningsCalendar"]:
        if not isinstance(item, dict) or str(item.get("symbol", "")).upper() != symbol:
            continue
        try:
            when = date.fromisoformat(str(item.get("date"))[:10])
        except ValueError:
            continue
        if start <= when <= end and (best is None or when < best[0]):
            best = (when, item)
    if best is None:
        raise _Unavailable(f"Finnhub has no earnings date for {ticker} in the window")
    when, item = best
    hour = str(item.get("hour") or "").lower()
    quarter, year = item.get("quarter"), item.get("year")
    return EarningsRow(
        source=FINNHUB_SOURCE,
        source_url=FINNHUB_DOCS_URL,
        retrieved_at=res.fetched_at,
        as_of=res.fetched_at.date(),
        stale=res.stale,
        notes=[NO_CONFIRMED_NOTE],
        ticker=ticker,
        method="finnhub",
        next_earnings_date=when,
        hour=hour if hour in HOURS else None,
        fiscal_quarter=quarter if isinstance(quarter, int) else None,
        fiscal_year=year if isinstance(year, int) else None,
        confirmed=None,
    )


async def _edgar_row(ticker: str, start: date, end: date, why: str) -> EarningsRow:
    fallback_note = f"Finnhub not used: {why}."
    try:
        company = await edgar.get_company(ticker)
        filings, _, sub = await edgar.list_filings_data(company.cik, forms=["8-K"])
    except (ToolError, ConfigError) as exc:
        return EarningsRow(
            source=EDGAR_SOURCE,
            source_url=edgar.TICKERS_URL,
            retrieved_at=utcnow(),
            as_of=None,
            notes=[fallback_note, f"No estimate: {exc}"],
            ticker=ticker,
            method="none",
        )
    earnings = [f for f in filings if "2.02" in (f.items or "").split(",")]
    est = estimate_next([f.filing_date for f in earnings], start, end)
    if est is None:
        latest = earnings[0] if earnings else None
        return EarningsRow(
            source=EDGAR_SOURCE,
            source_url=(latest.url if latest and latest.url else sub.url),
            retrieved_at=sub.fetched_at,
            as_of=latest.filing_date if latest else None,
            stale=sub.stale,
            notes=[
                fallback_note,
                "No estimated earnings date falls inside the window"
                + (f" (latest Item 2.02 8-K filed {latest.filing_date})." if latest else "."),
            ],
            ticker=ticker,
            method="none",
        )
    basis = next(f for f in earnings if f.filing_date == est.basis_filing)
    how = (
        f"8-K Item 2.02 filed {est.basis_filing} plus one year"
        if est.basis == "prior_year_same_quarter"
        else f"latest 8-K Item 2.02 filed {est.basis_filing} plus the recent quarterly cadence"
    )
    return EarningsRow(
        source=EDGAR_SOURCE,
        source_url=basis.url or sub.url,
        retrieved_at=sub.fetched_at,
        as_of=est.basis_filing,
        stale=sub.stale,
        notes=[
            fallback_note,
            f"Estimated, not announced: {how}. The company has not been checked for a date.",
        ],
        ticker=ticker,
        method="edgar_estimate",
        next_earnings_date=est.date,
        confirmed=False,
        estimated=True,
        estimate_basis=est.basis,
    )


async def _row(ticker: str, start: date, end: date) -> EarningsRow:
    try:
        return await _finnhub_row(ticker, start, end)
    except _Unavailable as exc:
        return await _edgar_row(ticker, start, end, str(exc).rstrip("."))


async def get_earnings_calendar(tickers: list[str], days_ahead: int = 30) -> EarningsCalendar:
    """Next earnings date per ticker within `days_ahead` days (1-180, max 20 tickers).

    Source: Finnhub earnings calendar; when Finnhub has nothing (or no key / unavailable) the
    date is estimated from prior SEC 8-K Item 2.02 filings and marked estimated=True.
    Each row names its own method and source. No EPS or revenue forecasts are returned.
    """
    if not isinstance(days_ahead, int) or not 1 <= days_ahead <= MAX_DAYS_AHEAD:
        raise BadInput(f"days_ahead must be between 1 and {MAX_DAYS_AHEAD}")
    symbols = _normalize(tickers)
    start = _today()
    end = start + timedelta(days=days_ahead)
    rows = list(await asyncio.gather(*(_row(t, start, end) for t in symbols)))
    sources = {r.source for r in rows}
    if len(sources) == 1:
        source = rows[0].source
        source_url = FINNHUB_DOCS_URL if source == FINNHUB_SOURCE else edgar.TICKERS_URL
    else:
        source = "Mixed: see each row's source"
        source_url = FINNHUB_DOCS_URL
    return EarningsCalendar(
        source=source,
        source_url=source_url,
        retrieved_at=max(r.retrieved_at for r in rows),
        as_of=start,
        stale=any(r.stale for r in rows),
        window_start=start,
        window_end=end,
        rows=rows,
    )


def register(mcp: FastMCP) -> None:
    mcp.tool()(get_earnings_calendar)
