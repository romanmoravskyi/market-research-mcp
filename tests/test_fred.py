import json
import logging
import os
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from mcp.server.fastmcp import FastMCP

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.config import ConfigError
from market_research_mcp.errors import BadInput, NotFound
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import fred

FIXTURES = Path(__file__).parent / "fixtures" / "fred"
SERIES_URL = "https://api.stlouisfed.org/fred/series"
OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
SECRET = "FREDSECRETKEY0123456789"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    real_key = os.environ.get("FRED_API_KEY")
    store = Cache(tmp_path / "cache.db")
    cache_mod.set_default_cache(store)

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr(http, "_sleep", fake_sleep)
    monkeypatch.setenv("FRED_API_KEY", SECRET)
    reset_limiters()
    yield real_key
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


def mock_fred(respx_mock, ids=("DGS10", "CPIAUCSL")):
    routes = {}
    for sid in ids:
        routes[(sid, "series")] = respx_mock.get(SERIES_URL, params={"series_id": sid}).mock(
            return_value=httpx.Response(200, json=load(f"{sid}_series.json"))
        )
        routes[(sid, "obs")] = respx_mock.get(OBS_URL, params={"series_id": sid}).mock(
            return_value=httpx.Response(200, json=load(f"{sid}_observations.json"))
        )
    return routes


@respx.mock(assert_all_mocked=True)
async def test_happy_path_two_series(respx_mock):
    mock_fred(respx_mock)
    result = await fred.get_macro_series(["DGS10", "cpiaucsl"])
    assert [s.id for s in result] == ["DGS10", "CPIAUCSL"]

    dgs = result[0]
    assert dgs.source == "FRED, Federal Reserve Bank of St. Louis"
    assert dgs.source_url == "https://fred.stlouisfed.org/series/DGS10"
    assert dgs.title.startswith("Market Yield on U.S. Treasury Securities at 10-Year")
    assert dgs.units == "Percent" and dgs.frequency == "Daily"
    assert dgs.last_updated == "2026-10-23 16:02:03-05"
    dates = [o.date for o in dgs.observations]
    assert dates == sorted(dates)  # newest last
    assert dgs.observations[-1].date == date(2026, 10, 22)
    assert dgs.observations[-1].value == pytest.approx(4.12)
    assert dgs.as_of == date(2026, 10, 22)
    assert not dgs.truncated and not dgs.stale
    assert any("third-party copyrighted" in n for n in dgs.notes)
    assert dgs.retrieved_at.tzinfo is not None

    cpi = result[1]
    assert cpi.units == "Index 1982-1984=100" and cpi.frequency == "Monthly"
    assert cpi.as_of == date(2026, 9, 1)


@respx.mock
async def test_request_params(respx_mock):
    routes = mock_fred(respx_mock, ids=("DGS10",))
    await fred.get_macro_series(["DGS10"], start="2026-01-01", limit=50)
    obs_req = routes[("DGS10", "obs")].calls.last.request
    params = obs_req.url.params
    assert params["file_type"] == "json"
    assert params["observation_start"] == "2026-01-01"
    assert params["sort_order"] == "desc" and params["limit"] == "50"
    assert params["api_key"] == SECRET


@respx.mock
async def test_dot_value_is_none(respx_mock):
    mock_fred(respx_mock, ids=("DGS10",))
    (dgs,) = await fred.get_macro_series(["DGS10"])
    by_date = {o.date: o.value for o in dgs.observations}
    assert by_date[date(2026, 10, 13)] is None
    assert by_date[date(2026, 10, 10)] == pytest.approx(4.05)


@respx.mock
async def test_limit_truncates_to_newest(respx_mock):
    mock_fred(respx_mock, ids=("DGS10",))
    (dgs,) = await fred.get_macro_series(["DGS10"], limit=2)
    assert [o.date for o in dgs.observations] == [date(2026, 10, 21), date(2026, 10, 22)]
    assert dgs.truncated and dgs.limit == 2
    assert dgs.as_of == date(2026, 10, 22)


async def test_missing_key_raises_config_error(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="FRED_API_KEY"):
        await fred.get_macro_series(["DGS10"])


@pytest.mark.parametrize(
    "ids,kwargs",
    [
        (["DGS-10"], {}),
        (["DGS10; DROP"], {}),
        ([""], {}),
        ([], {}),
        ([f"S{i}" for i in range(11)], {}),
        (["DGS10"], {"limit": 0}),
        (["DGS10"], {"start": "2026/01/01"}),
    ],
)
async def test_bad_input(ids, kwargs):
    with pytest.raises(BadInput):
        await fred.get_macro_series(ids, **kwargs)


@respx.mock
async def test_unknown_id_not_found(respx_mock):
    respx_mock.get(SERIES_URL).mock(
        return_value=httpx.Response(400, json=load("error_unknown_series.json"))
    )
    with pytest.raises(NotFound) as ei:
        await fred.get_macro_series(["NOSUCHSERIES"])
    assert "NOSUCHSERIES" in str(ei.value)
    assert SECRET not in str(ei.value)


@respx.mock
async def test_key_never_leaks(respx_mock, caplog):
    caplog.set_level(logging.DEBUG)
    mock_fred(respx_mock, ids=("DGS10",))
    (dgs,) = await fred.get_macro_series(["DGS10"])
    assert "api_key" not in dgs.source_url
    assert SECRET not in dgs.model_dump_json()
    assert SECRET not in caplog.text


@respx.mock
async def test_cache_reuse(respx_mock, env):
    routes = mock_fred(respx_mock, ids=("DGS10",))
    first = await fred.get_macro_series(["DGS10"])
    second = await fred.get_macro_series(["DGS10"])
    assert first[0].observations == second[0].observations
    assert routes[("DGS10", "series")].call_count == 1
    assert routes[("DGS10", "obs")].call_count == 1
    store = cache_mod.get_cache()
    with store._connect() as conn:
        rows = conn.execute("SELECT namespace, key FROM cache").fetchall()
    assert rows and all(ns == "fred" and SECRET not in key for ns, key in rows)


async def test_registers_tool():
    mcp = FastMCP("t")
    fred.register(mcp)
    assert "get_macro_series" in {t.name for t in await mcp.list_tools()}


@pytest.mark.live
async def test_live_dgs10(env, monkeypatch):
    real_key = env
    if not real_key:
        pytest.skip("FRED_API_KEY not set")
    monkeypatch.setenv("FRED_API_KEY", real_key)
    (dgs,) = await fred.get_macro_series(["DGS10"], limit=5)
    assert dgs.id == "DGS10" and dgs.units == "Percent"
    assert 1 <= len(dgs.observations) <= 5
    assert real_key not in dgs.model_dump_json()


@respx.mock
async def test_bad_api_key_is_config_error(respx_mock):
    respx_mock.get(SERIES_URL).mock(
        return_value=httpx.Response(400, json=load("error_bad_api_key.json"))
    )
    with pytest.raises(ConfigError, match="FRED_API_KEY") as ei:
        await fred.get_macro_series(["DGS10"])
    assert SECRET not in str(ei.value)


@respx.mock
async def test_series_fetched_concurrently_order_preserved(respx_mock):
    import asyncio

    second_started = asyncio.Event()
    routes = mock_fred(respx_mock)
    dgs_side = routes[("DGS10", "series")].return_value
    cpi_side = routes[("CPIAUCSL", "series")].return_value

    async def slow_first(request):
        await second_started.wait()  # deadlocks if series are fetched one after another
        return dgs_side

    async def mark_second(request):
        second_started.set()
        return cpi_side

    routes[("DGS10", "series")].side_effect = slow_first
    routes[("CPIAUCSL", "series")].side_effect = mark_second
    result = await asyncio.wait_for(fred.get_macro_series(["DGS10", "CPIAUCSL"]), timeout=5)
    assert [s.id for s in result] == ["DGS10", "CPIAUCSL"]
