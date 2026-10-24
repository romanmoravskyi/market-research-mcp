"""Daily price history and price statistics.

One provider per call, end to end: there is no silent cross-provider fallback. Tiingo is the
default (`PRICE_PROVIDER=tiingo`, key `TIINGO_API_KEY` sent in the Authorization header).
yfinance is opt-in (`PRICE_PROVIDER=yfinance`), unofficial, and imported lazily.

Read-only: these tools describe past prices. They never recommend buying or selling.
"""

from __future__ import annotations

import asyncio
import math
import statistics
from datetime import date, timedelta
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from market_research_mcp import http
from market_research_mcp.cache import seconds_until_next_us_close
from market_research_mcp.config import ConfigError, get_settings, require
from market_research_mcp.errors import BadInput, NotFound, ProviderDown
from market_research_mcp.models import Sourced, utcnow
from market_research_mcp.tickers import normalize_ticker

__all__ = [
    "Bar",
    "PriceHistory",
    "PriceProvider",
    "PriceStats",
    "Split",
    "TiingoProvider",
    "YFinanceProvider",
    "annualized_volatility",
    "get_price_history",
    "get_price_stats",
    "get_provider",
    "get_split_history",
    "max_drawdown",
    "period_return",
    "normalize_ticker",
    "register",
    "splits_from_bars",
]

TIINGO_BASE = "https://api.tiingo.com/tiingo/daily"
SPLIT_WINDOW_YEARS = 10  # default split lookback: covers the fundamentals horizon

RETURN_WINDOWS = {"1m": 21, "3m": 63, "1y": 252, "5y": 1260}
YEAR_BARS = 252
FIVE_YEAR_BARS = 1260
MAX_LIMIT = 5000

Date = date  # alias so model fields named `date` don't shadow the type


# ---------------------------------------------------------------- models


class Bar(BaseModel):
    """One daily bar. Adjusted fields are split- and dividend-adjusted by the provider."""

    date: Date
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj_open: float | None = None
    adj_high: float | None = None
    adj_low: float | None = None
    adj_close: float
    split_factor: float = 1.0
    div_cash: float = 0.0


class Split(BaseModel):
    """A stock split. `factor` = new shares per old share (4.0 for a 4:1 split)."""

    date: Date
    factor: float


class HistoryBar(BaseModel):
    date: Date
    open: float
    high: float
    low: float
    close: float
    volume: float
    adj_close: float


class PriceHistory(Sourced):
    ticker: str
    provider: str
    interval: str
    bars: list[HistoryBar]
    limit: int
    truncated: bool = False


class Drawdown(BaseModel):
    value: float | None
    peak_date: Date | None
    trough_date: Date | None


class PriceStats(Sourced):
    ticker: str
    provider: str
    last_adj_close: float | None
    returns: dict[str, float | None]
    max_drawdown_5y: Drawdown
    high_52w: float | None
    low_52w: float | None
    volatility_1y: float | None


# ---------------------------------------------------------------- providers


class PriceProvider(Protocol):
    name: str

    async def history(self, ticker: str, start: date, end: date) -> list[Bar]: ...

    async def splits(self, ticker: str, start: date, end: date) -> list[Split]: ...

    def source_url(self, ticker: str) -> str: ...


def splits_from_bars(bars: list[Bar]) -> list[Split]:
    """Splits recorded in already-fetched bars (split_factor != 1), oldest first."""
    out = [Split(date=b.date, factor=b.split_factor) for b in bars if b.split_factor != 1.0]
    out.sort(key=lambda s: s.date)
    return out


def _parse_date(value: str | None, field: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BadInput(f"{field} must be a date in YYYY-MM-DD format, got {value!r}.") from None


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


class TiingoProvider:
    name = "tiingo"
    label = "Tiingo"
    alternative = "yfinance (set PRICE_PROVIDER=yfinance; unofficial source)"

    def _headers(self) -> dict[str, str]:
        key = require("TIINGO_API_KEY")
        return {"Authorization": f"Token {key}", "Content-Type": "application/json"}

    def source_url(self, ticker: str) -> str:
        return f"{TIINGO_BASE}/{ticker.lower()}/prices"

    async def _prices(self, ticker: str, start: date, end: date | None) -> list[dict[str, Any]]:
        headers = self._headers()
        params = {"startDate": start.isoformat(), "format": "json"}
        if end is not None:
            params["endDate"] = end.isoformat()
        result = await http.fetch(
            "tiingo",
            self.source_url(ticker),
            params=params,
            headers=headers,
            cache_ns="prices",
            cache_key=f"tiingo:{ticker}:{start.isoformat()}:{end.isoformat() if end else ''}",
            ttl=seconds_until_next_us_close(),
            alternative=self.alternative,
        )
        data = result.data
        if not isinstance(data, list):
            raise ProviderDown(self.label, "unexpected response format", self.alternative)
        return data

    async def history(self, ticker: str, start: date, end: date) -> list[Bar]:
        rows = await self._prices(ticker, start, end)
        try:
            bars = [
                Bar(
                    date=date.fromisoformat(str(r["date"])[:10]),
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=r["volume"],
                    adj_open=_float(r.get("adjOpen")),
                    adj_high=_float(r.get("adjHigh")),
                    adj_low=_float(r.get("adjLow")),
                    adj_close=r["adjClose"],
                    split_factor=r.get("splitFactor") or 1.0,
                    div_cash=r.get("divCash") or 0.0,
                )
                for r in rows
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderDown(self.label, "unexpected response format", self.alternative) from exc
        bars.sort(key=lambda b: b.date)
        return bars

    async def splits(self, ticker: str, start: date, end: date) -> list[Split]:
        # Same request (and cache key) as history(ticker, start, end): one HTTP call serves both.
        return splits_from_bars(await self.history(ticker, start, end))


class YFinanceProvider:
    """Unofficial Yahoo Finance data via the `yfinance` package (opt-in extra)."""

    name = "yfinance"
    label = "yfinance (unofficial Yahoo Finance)"
    alternative = "Tiingo (set PRICE_PROVIDER=tiingo and TIINGO_API_KEY)"

    def __init__(self) -> None:
        self._yf = self._import()

    @staticmethod
    def _import() -> Any:
        try:
            import yfinance  # type: ignore[import-not-found]
        except ImportError:
            raise ConfigError("Install the yfinance extra: uv sync --extra yfinance") from None
        return yfinance

    def source_url(self, ticker: str) -> str:
        return f"https://finance.yahoo.com/quote/{ticker}/history"

    def _history_blocking(self, ticker: str, start: date, end: date) -> list[Bar]:
        df = self._yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),  # yfinance `end` is exclusive
            interval="1d",
            auto_adjust=False,
            actions=True,
        )
        bars = []
        for idx, row in df.iterrows():
            close = float(row["Close"])
            adj_close = float(row.get("Adj Close", close))
            ratio = adj_close / close if close else 1.0
            split = float(row.get("Stock Splits", 0.0) or 0.0)
            bars.append(
                Bar(
                    date=idx.date(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=close,
                    volume=float(row["Volume"]),
                    adj_open=float(row["Open"]) * ratio,
                    adj_high=float(row["High"]) * ratio,
                    adj_low=float(row["Low"]) * ratio,
                    adj_close=adj_close,
                    split_factor=split if split else 1.0,
                    div_cash=float(row.get("Dividends", 0.0) or 0.0),
                )
            )
        return bars

    def _splits_blocking(self, ticker: str, start: date, end: date) -> list[Split]:
        series = self._yf.Ticker(ticker).splits
        return [
            Split(date=idx.date(), factor=float(v))
            for idx, v in series.items()
            if float(v) != 1.0 and start <= idx.date() <= end
        ]

    async def history(self, ticker: str, start: date, end: date) -> list[Bar]:
        try:
            return await asyncio.to_thread(self._history_blocking, ticker, start, end)
        except Exception as exc:  # yfinance raises many ad-hoc exception types
            raise ProviderDown(self.label, type(exc).__name__, self.alternative) from exc

    async def splits(self, ticker: str, start: date, end: date) -> list[Split]:
        try:
            return await asyncio.to_thread(self._splits_blocking, ticker, start, end)
        except Exception as exc:
            raise ProviderDown(self.label, type(exc).__name__, self.alternative) from exc


def get_provider(name: str | None = None) -> TiingoProvider | YFinanceProvider:
    """Provider selected by `PRICE_PROVIDER` (default tiingo). Never falls back silently."""
    chosen = (name or get_settings().price_provider or "tiingo").strip().lower()
    if chosen == "tiingo":
        return TiingoProvider()
    if chosen == "yfinance":
        return YFinanceProvider()
    raise ConfigError(f"PRICE_PROVIDER must be 'tiingo' or 'yfinance', got {chosen!r}.")


# ---------------------------------------------------------------- pure math


def period_return(adj_closes: list[float], offset: int) -> float | None:
    """Simple return over `offset` trading days: last / value `offset` bars earlier - 1."""
    if offset <= 0 or len(adj_closes) <= offset:
        return None
    base = adj_closes[-1 - offset]
    if base == 0:
        return None
    return adj_closes[-1] / base - 1


def max_drawdown(bars: list[Bar]) -> Drawdown:
    """Largest peak-to-trough fall in adj_close, as a negative fraction (-0.25 = -25%)."""
    if len(bars) < 2:
        return Drawdown(value=None, peak_date=None, trough_date=None)
    peak = bars[0]
    worst = 0.0
    worst_peak = worst_trough = None
    for bar in bars[1:]:
        if bar.adj_close > peak.adj_close:
            peak = bar
            continue
        dd = bar.adj_close / peak.adj_close - 1 if peak.adj_close else 0.0
        if dd < worst:
            worst, worst_peak, worst_trough = dd, peak.date, bar.date
    return Drawdown(value=worst, peak_date=worst_peak, trough_date=worst_trough)


def annualized_volatility(adj_closes: list[float], window: int = YEAR_BARS) -> float | None:
    """Sample stdev of daily log returns over the last `window` returns, times sqrt(252)."""
    if len(adj_closes) < window + 1:
        return None
    series = adj_closes[-(window + 1) :]
    if any(v <= 0 for v in series):
        return None
    log_returns = [math.log(b / a) for a, b in zip(series, series[1:], strict=False)]
    return statistics.stdev(log_returns) * math.sqrt(YEAR_BARS)


# ---------------------------------------------------------------- tools


def _today() -> date:
    return utcnow().date()


async def get_price_history(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    interval: str = "1d",
    limit: int = 260,
) -> PriceHistory:
    """Daily price bars (OHLCV plus adjusted close) for a US ticker from one price provider.

    Only interval "1d" is supported. Dates are YYYY-MM-DD. Returns at most `limit` most recent
    bars in the range. The response names the provider; there is no fallback to another one.
    """
    if interval != "1d":
        raise BadInput(f"interval {interval!r} is not supported; only '1d' (daily) is.")
    if not 1 <= limit <= MAX_LIMIT:
        raise BadInput(f"limit must be between 1 and {MAX_LIMIT}.")
    symbol = normalize_ticker(ticker)
    end_d = _parse_date(end, "end") or _today()
    start_d = _parse_date(start, "start") or end_d - timedelta(days=math.ceil(limit * 7 / 5) + 15)
    if start_d > end_d:
        raise BadInput("start must be on or before end.")

    provider = get_provider()
    bars = await provider.history(symbol, start_d, end_d)
    if not bars:
        raise NotFound(f"price history for {symbol} {start_d}..{end_d}", provider.label)
    truncated = len(bars) > limit
    bars = bars[-limit:]
    notes = [f"Prices from {provider.label}; adj_close is split- and dividend-adjusted."]
    if provider.name == "yfinance":
        notes.append("yfinance is an unofficial source; verify important figures.")
    return PriceHistory(
        source=provider.label,
        source_url=provider.source_url(symbol),
        retrieved_at=utcnow(),
        as_of=bars[-1].date,
        notes=notes,
        ticker=symbol,
        provider=provider.name,
        interval=interval,
        bars=[HistoryBar(**b.model_dump(include=set(HistoryBar.model_fields))) for b in bars],
        limit=limit,
        truncated=truncated,
    )


def compute_stats(bars: list[Bar]) -> tuple[dict[str, Any], list[str]]:
    """Statistics from one provider's adjusted series. Returns (fields, notes)."""
    notes: list[str] = []
    adj = [b.adj_close for b in bars]
    returns = {k: period_return(adj, n) for k, n in RETURN_WINDOWS.items()}
    for k, n in RETURN_WINDOWS.items():
        if returns[k] is None:
            notes.append(f"{k} return is null: needs {n + 1} trading days, have {len(adj)}.")

    window_5y = bars[-(FIVE_YEAR_BARS + 1) :]
    if len(bars) < FIVE_YEAR_BARS + 1:
        notes.append(
            f"Max drawdown uses all {len(bars)} available trading days (less than 5 years)."
        )
    drawdown = max_drawdown(window_5y)

    year = bars[-YEAR_BARS:]
    highs = [b.adj_high if b.adj_high is not None else b.adj_close for b in year]
    lows = [b.adj_low if b.adj_low is not None else b.adj_close for b in year]
    notes.append(
        "52-week high/low use split-adjusted intraday highs/lows (adjHigh/adjLow) over the last "
        f"{len(year)} trading days."
    )
    if len(year) < YEAR_BARS:
        notes.append(f"52-week window is short: only {len(year)} trading days available.")

    vol = annualized_volatility(adj)
    if vol is None:
        notes.append(f"Volatility is null: needs {YEAR_BARS + 1} trading days, have {len(adj)}.")
    else:
        notes.append("Volatility = stdev of daily log returns over 252 days x sqrt(252).")

    fields = {
        "last_adj_close": adj[-1] if adj else None,
        "returns": returns,
        "max_drawdown_5y": drawdown,
        "high_52w": max(highs) if highs else None,
        "low_52w": min(lows) if lows else None,
        "volatility_1y": vol,
    }
    return fields, notes


async def get_price_stats(ticker: str) -> PriceStats:
    """Price statistics from one provider's adjusted closes: 1m/3m/1y/5y returns (21/63/252/1260
    trading days), 5-year max drawdown with peak and trough dates, 52-week high/low and 1-year
    annualized volatility. Descriptive only; not a recommendation.
    """
    symbol = normalize_ticker(ticker)
    end_d = _today()
    start_d = end_d - timedelta(days=5 * 366 + 45)  # > 1261 trading days
    provider = get_provider()
    bars = await provider.history(symbol, start_d, end_d)
    if not bars:
        raise NotFound(f"price history for {symbol}", provider.label)
    fields, notes = compute_stats(bars)
    notes.insert(0, f"All statistics computed from {provider.label} adjusted prices only.")
    return PriceStats(
        source=provider.label,
        source_url=provider.source_url(symbol),
        retrieved_at=utcnow(),
        as_of=bars[-1].date,
        notes=notes,
        ticker=symbol,
        provider=provider.name,
        **fields,
    )


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


async def get_split_history(
    ticker: str,
    start: date | None = None,
    end: date | None = None,
    *,
    bars: list[Bar] | None = None,
) -> list[Split]:
    """Public helper (not an MCP tool): stock splits for `ticker`, oldest first.

    Used by the XBRL module to adjust share counts, which only needs the fundamentals'
    horizon, so the window defaults to the last 10 years (`start` = end - 10 years,
    `end` = today) instead of the full listed history. With Tiingo, a call with the same
    start/end as an earlier `history()` call is served from that cached response (no extra
    HTTP). Pass `bars` already fetched to derive splits from them with no request at all.
    Uses the configured price provider.
    """
    if bars is not None:
        return splits_from_bars(bars)
    end_d = end or _today()
    start_d = start or _years_before(end_d, SPLIT_WINDOW_YEARS)
    return await get_provider().splits(normalize_ticker(ticker), start_d, end_d)


def register(mcp: FastMCP) -> None:
    mcp.tool()(get_price_history)
    mcp.tool()(get_price_stats)
