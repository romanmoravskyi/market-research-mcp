import logging
from datetime import timedelta

import httpx
import pytest
import respx

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.config import ConfigError
from market_research_mcp.errors import NotFound, ProviderDown, RateLimited
from market_research_mcp.models import utcnow
from market_research_mcp.ratelimit import reset_limiters

URL = "https://api.example.test/data"
SECRET = "SUPERSECRETVALUE42"


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    store = Cache(tmp_path / "cache.db")
    cache_mod.set_default_cache(store)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(http, "_sleep", fake_sleep)
    monkeypatch.setenv("SEC_USER_AGENT", "Test Agent test@example.test")
    reset_limiters()
    yield store, sleeps
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


@respx.mock(assert_all_mocked=True)
async def test_fetch_json_and_cache_hit(respx_mock, env):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    r1 = await http.fetch("fred", URL, cache_ns="fred", cache_key="k")
    assert r1.data == {"a": 1} and not r1.from_cache and not r1.stale
    r2 = await http.fetch("fred", URL, cache_ns="fred", cache_key="k")
    assert r2.data == {"a": 1} and r2.from_cache and not r2.stale
    assert route.call_count == 1


@respx.mock
async def test_parse_text_and_bytes(respx_mock):
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=b"hello"))
    assert (await http.fetch("x", URL, parse="text")).data == "hello"
    assert (await http.fetch("x", URL, parse="bytes")).data == b"hello"


@respx.mock
async def test_bad_json_is_provider_down(respx_mock):
    respx_mock.get(URL).mock(return_value=httpx.Response(200, content=b"<html>"))
    with pytest.raises(ProviderDown):
        await http.fetch("x", URL)


@respx.mock
async def test_ttl_expiry_refetches(respx_mock, env):
    store, _ = env
    old = utcnow() - timedelta(hours=2)
    store.set("news", "k", b'{"v": "old"}', 3600, now=old)
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, json={"v": "new"}))
    r = await http.fetch("finnhub", URL, cache_ns="news", cache_key="k")
    assert r.data == {"v": "new"} and not r.from_cache
    assert route.call_count == 1
    assert not store.get("news", "k").expired


@respx.mock
async def test_explicit_ttl_overrides_namespace(respx_mock, env):
    store, _ = env
    respx_mock.get(URL).mock(return_value=httpx.Response(200, json={}))
    await http.fetch("x", URL, cache_ns="news", cache_key="k", ttl=5)
    hit = store.get("news", "k")
    assert hit.expires_at - hit.fetched_at == timedelta(seconds=5)


@respx.mock
async def test_429_then_serves_stale(respx_mock, env):
    store, sleeps = env
    store.set("prices", "AAPL", b'{"close": 1}', 60, now=utcnow() - timedelta(days=1))
    route = respx_mock.get(URL).mock(return_value=httpx.Response(429))
    r = await http.fetch("tiingo", URL, cache_ns="prices", cache_key="AAPL")
    assert r.data == {"close": 1} and r.stale and r.from_cache
    assert route.call_count == 2  # one back-off retry
    assert len(sleeps) == 1


@respx.mock
async def test_429_recovers_after_backoff(respx_mock):
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json=1)]
    )
    r = await http.fetch("x", URL)
    assert r.data == 1 and not r.stale


@respx.mock
async def test_429_without_cache_raises_rate_limited(respx_mock, env):
    _, sleeps = env
    respx_mock.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimited) as ei:
        await http.fetch("finnhub", URL, cache_ns="news", cache_key="none")
    assert "finnhub" in str(ei.value)
    assert sleeps == [http.MAX_RETRY_AFTER_SECONDS]  # Retry-After capped


@respx.mock
async def test_404_not_found(respx_mock):
    respx_mock.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound):
        await http.fetch("edgar", URL)


@respx.mock
async def test_5xx_retries_then_provider_down(respx_mock, env):
    _, sleeps = env
    route = respx_mock.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderDown) as ei:
        await http.fetch("tiingo", URL, alternative="finnhub")
    assert route.call_count == 3
    assert len(sleeps) == 2
    assert ei.value.alternative == "finnhub" and "finnhub" in str(ei.value)


@respx.mock
async def test_5xx_then_success(respx_mock):
    respx_mock.get(URL).mock(side_effect=[httpx.Response(502), httpx.Response(200, json=[1])])
    assert (await http.fetch("x", URL)).data == [1]


@respx.mock
async def test_connect_error_retries_then_provider_down(respx_mock):
    route = respx_mock.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderDown):
        await http.fetch("x", URL)
    assert route.call_count == 3


@respx.mock
async def test_other_4xx_provider_down(respx_mock):
    respx_mock.get(URL).mock(return_value=httpx.Response(403))
    with pytest.raises(ProviderDown, match="HTTP 403"):
        await http.fetch("x", URL)


@respx.mock
async def test_edgar_sets_user_agent(respx_mock):
    route = respx_mock.get("https://data.sec.gov/x.json").mock(
        return_value=httpx.Response(200, json={})
    )
    await http.fetch("edgar", "https://data.sec.gov/x.json")
    req = route.calls.last.request
    assert req.headers["User-Agent"] == "Test Agent test@example.test"
    assert "gzip" in req.headers["Accept-Encoding"]


async def test_edgar_without_user_agent_raises_config_error(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ConfigError, match="SEC_USER_AGENT"):
        await http.fetch("edgar", "https://data.sec.gov/x.json")


@respx.mock
async def test_redacted_url_never_contains_key(respx_mock, caplog):
    respx_mock.get(URL).mock(return_value=httpx.Response(503))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ProviderDown) as ei:
        await http.fetch("fred", URL, params={"series_id": "DGS10", "api_key": SECRET})
    assert SECRET not in str(ei.value)
    assert SECRET not in caplog.text
    assert "api_key=***" in caplog.text

    respx_mock.get(URL).mock(return_value=httpx.Response(200, json={}))
    r = await http.fetch("tiingo", URL, params={"token": SECRET})
    assert SECRET not in r.url_redacted and "token=***" in r.url_redacted
    assert SECRET not in caplog.text


@respx.mock
async def test_not_found_message_redacted(respx_mock):
    respx_mock.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(NotFound) as ei:
        await http.fetch("finnhub", URL, params={"token": SECRET})
    assert SECRET not in str(ei.value)
