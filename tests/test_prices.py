import gzip
import json
import math
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from mcp.server.fastmcp import FastMCP

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.config import ConfigError, Settings, load_settings
from market_research_mcp.errors import BadInput, ProviderDown
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import prices
from market_research_mcp.sources.prices import (
    Bar,
    Split,
    annualized_volatility,
    compute_stats,
    max_drawdown,
    period_return,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tiingo"
PRICES_URL = "https://api.tiingo.com/tiingo/daily/msft/prices"
KEY = "TIINGOTESTKEY0123456789"


def load_rows() -> list[dict]:
    return json.loads(gzip.decompress((FIXTURES / "msft_prices.json.gz").read_bytes()))


ROWS = load_rows()


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    cache_mod.set_default_cache(Cache(tmp_path / "cache.db"))

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(http, "_sleep", fake_sleep)
    monkeypatch.setattr(prices, "get_settings", lambda: Settings(price_provider="tiingo"))
    monkeypatch.setenv("TIINGO_API_KEY", KEY)
    reset_limiters()
    yield
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


def use_provider(monkeypatch, name: str) -> None:
    monkeypatch.setattr(prices, "get_settings", lambda: Settings(price_provider=name))


def mk_bars(adj: list[float]) -> list[Bar]:
    return [
        Bar(
            date=date(2026, 1, i + 1),
            open=v,
            high=v,
            low=v,
            close=v,
            volume=1,
            adj_close=v,
        )
        for i, v in enumerate(adj)
    ]


# ---------------------------------------------------------------- fixture sanity


def test_fixture_is_synthetic_shape():
    meta = json.loads((FIXTURES / "msft_meta.json").read_text())
    assert set(meta) == {"ticker", "name", "exchangeCode", "startDate", "endDate", "description"}
    assert "Synthetic" in meta["description"]
    assert set(ROWS[0]) == {
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adjOpen",
        "adjHigh",
        "adjLow",
        "adjClose",
        "adjVolume",
        "divCash",
        "splitFactor",
    }
    assert len(ROWS) > 1500
    assert [r["date"][:10] for r in ROWS if r["splitFactor"] != 1] == ["2023-03-15"]


# ---------------------------------------------------------------- history


@respx.mock
async def test_history_parse_and_limit(respx_mock):
    respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=ROWS))
    res = await prices.get_price_history("MSFT", start="2020-01-01", end="2026-10-23", limit=10)
    assert res.provider == "tiingo" and res.source == "Tiingo"
    assert len(res.bars) == 10 and res.truncated
    last = ROWS[-1]
    assert res.as_of == date.fromisoformat(last["date"][:10]) == res.bars[-1].date
    assert res.bars[-1].close == last["close"]
    assert res.bars[-1].adj_close == last["adjClose"]
    assert res.bars[-1].volume == last["volume"]
    assert set(res.bars[0].model_dump()) == {
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "adj_close",
    }


@respx.mock
async def test_history_not_truncated_when_under_limit(respx_mock):
    respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=ROWS[-5:]))
    res = await prices.get_price_history("msft", limit=260)
    assert len(res.bars) == 5 and not res.truncated


async def test_bad_interval():
    with pytest.raises(BadInput, match="1d"):
        await prices.get_price_history("MSFT", interval="1wk")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": "2024/01/01"},
        {"start": "2025-01-01", "end": "2024-01-01"},
        {"limit": 0},
        {"ticker": "BAD TICKER!"},
    ],
)
async def test_bad_inputs(kwargs):
    args = {"ticker": "MSFT", **kwargs}
    with pytest.raises(BadInput):
        await prices.get_price_history(**args)


@respx.mock
async def test_dotted_ticker_uses_dash(respx_mock):
    route = respx_mock.get("https://api.tiingo.com/tiingo/daily/brk-b/prices").mock(
        return_value=httpx.Response(200, json=ROWS[-3:])
    )
    res = await prices.get_price_history("brk.b")
    assert route.called and res.ticker == "BRK-B"


@respx.mock
async def test_auth_header_and_key_not_leaked(respx_mock):
    route = respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=ROWS[-3:]))
    res = await prices.get_price_history("MSFT")
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Token {KEY}"
    assert KEY not in str(req.url)
    assert KEY not in res.model_dump_json()


async def test_missing_key_raises_config_error(monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="TIINGO_API_KEY"):
        await prices.get_price_history("MSFT")


@respx.mock
async def test_provider_down_names_alternative(respx_mock):
    respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderDown) as ei:
        await prices.get_price_history("MSFT")
    assert "yfinance" in ei.value.alternative and "yfinance" in str(ei.value)
    assert KEY not in str(ei.value)


@respx.mock
async def test_unexpected_payload_is_provider_down(respx_mock):
    respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json={"detail": "x"}))
    with pytest.raises(ProviderDown, match="yfinance"):
        await prices.get_price_history("MSFT")


async def test_yfinance_selected_but_missing(monkeypatch):
    use_provider(monkeypatch, "yfinance")
    monkeypatch.setitem(sys.modules, "yfinance", None)  # makes `import yfinance` fail
    with pytest.raises(ConfigError, match="uv sync --extra yfinance"):
        await prices.get_price_history("MSFT")


async def test_unknown_provider(monkeypatch):
    use_provider(monkeypatch, "bloomberg")
    with pytest.raises(ConfigError, match="PRICE_PROVIDER"):
        prices.get_provider()


# ---------------------------------------------------------------- math on a tiny series

TINY = [100.0, 110.0, 99.0, 121.0, 96.8]


def test_period_return_hand_computed():
    assert period_return(TINY, 2) == pytest.approx(96.8 / 99 - 1)  # -0.022222
    assert period_return(TINY, 4) == pytest.approx(-0.032)
    assert period_return(TINY, 5) is None


def test_max_drawdown_value_and_dates():
    dd = max_drawdown(mk_bars(TINY))
    # 110 -> 99 is -10 %; 121 -> 96.8 is -20 % (the worst)
    assert dd.value == pytest.approx(-0.2)
    assert dd.peak_date == date(2026, 1, 4) and dd.trough_date == date(2026, 1, 5)


def test_max_drawdown_rising_series_is_zero():
    dd = max_drawdown(mk_bars([1.0, 2.0, 3.0]))
    assert dd.value == 0.0 and dd.peak_date is None


def test_volatility_hand_computed():
    # stdev of two log returns = |r1 - r2| / sqrt(2)
    expected = abs(math.log(1.1) - math.log(0.9)) / math.sqrt(2) * math.sqrt(252)
    assert annualized_volatility(TINY[:3], window=2) == pytest.approx(expected)  # ~2.2525
    assert annualized_volatility(TINY[:2], window=2) is None


def test_compute_stats_short_history_nulls_with_notes():
    fields, notes = compute_stats(mk_bars(TINY))
    assert all(v is None for v in fields["returns"].values())
    assert fields["volatility_1y"] is None
    assert fields["high_52w"] == 121.0 and fields["low_52w"] == 96.8
    text = " ".join(notes)
    assert "1y return is null" in text and "5y return is null" in text
    assert "Volatility is null" in text and "less than 5 years" in text


# ---------------------------------------------------------------- stats tool on fixture


@respx.mock
async def test_price_stats_on_fixture(respx_mock):
    respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=ROWS))
    res = await prices.get_price_stats("MSFT")
    adj = [r["adjClose"] for r in ROWS]
    assert res.provider == "tiingo"
    assert res.as_of == date.fromisoformat(ROWS[-1]["date"][:10])
    for key, n in {"1m": 21, "3m": 63, "1y": 252, "5y": 1260}.items():
        assert res.returns[key] == pytest.approx(adj[-1] / adj[-1 - n] - 1)
    assert res.high_52w == max(r["adjHigh"] for r in ROWS[-252:])
    assert res.low_52w == min(r["adjLow"] for r in ROWS[-252:])
    assert res.volatility_1y == pytest.approx(annualized_volatility(adj))
    dd = res.max_drawdown_5y
    assert dd.value < 0 and dd.peak_date < dd.trough_date
    assert any("adjHigh/adjLow" in n for n in res.notes)


@respx.mock
async def test_price_stats_too_short(respx_mock):
    respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=ROWS[-100:]))
    res = await prices.get_price_stats("MSFT")
    assert res.returns["3m"] is not None
    assert res.returns["1y"] is None and res.returns["5y"] is None
    assert res.volatility_1y is None
    assert any("1y return is null" in n for n in res.notes)


# ---------------------------------------------------------------- splits


@respx.mock
async def test_split_history(respx_mock):
    route = respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=ROWS))
    splits = await prices.get_split_history("MSFT")
    assert splits == [Split(date=date(2023, 3, 15), factor=4.0)]
    assert route.call_count == 1


async def test_tools_registered():
    mcp = FastMCP("t")
    prices.register(mcp)
    names = {t.name for t in await mcp.list_tools()}
    assert {"get_price_history", "get_price_stats"} <= names


# ---------------------------------------------------------------- live


@pytest.mark.live
async def test_live_tiingo_history(monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)  # drop the fake key; read .env
    if not load_settings().tiingo_api_key:
        pytest.skip("TIINGO_API_KEY not set")
    res = await prices.get_price_history("MSFT", limit=5)
    assert res.provider == "tiingo" and len(res.bars) == 5


@respx.mock
async def test_split_history_default_window_is_ten_years(respx_mock, monkeypatch):
    monkeypatch.setattr(prices, "_today", lambda: date(2026, 10, 23))
    route = respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=ROWS))
    await prices.get_split_history("MSFT")
    assert route.calls.last.request.url.params["startDate"] == "2016-10-23"


@respx.mock
async def test_split_history_shares_history_request(respx_mock):
    route = respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=ROWS))
    start, end = date(2020, 1, 1), date(2026, 10, 23)
    await prices.get_provider().history("MSFT", start, end)
    splits = await prices.get_split_history("MSFT", start=start, end=end)
    assert splits == [Split(date=date(2023, 3, 15), factor=4.0)]
    assert route.call_count == 1


def test_split_history_from_bars_needs_no_http():
    bars = mk_bars([1.0, 2.0, 3.0])
    bars[1] = bars[1].model_copy(update={"split_factor": 2.0})
    assert prices.splits_from_bars(bars) == [Split(date=bars[1].date, factor=2.0)]


@respx.mock
async def test_split_history_tolerates_rows_without_ohlc(respx_mock):
    rows = [
        {k: v for k, v in r.items() if k not in ("open", "high", "low", "volume")} for r in ROWS
    ]
    respx_mock.get(PRICES_URL).mock(return_value=httpx.Response(200, json=rows))
    splits = await prices.get_split_history("MSFT")
    assert splits == [Split(date=date(2023, 3, 15), factor=4.0)]


async def test_split_history_has_no_dead_bars_param():
    with pytest.raises(TypeError):
        await prices.get_split_history("MSFT", bars=[])
