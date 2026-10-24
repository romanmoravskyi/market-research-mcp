"""Tests for 10-K/10-Q section and exhibit tools. No network: SEC is mocked with respx and
served from gzip fixtures in tests/fixtures/sec/docs/ (see scripts/record_doc_fixtures.py)."""

import gzip
import json
import os
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.errors import BadInput, NotFound
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import edgar, edgar_docs

DOCS = Path(__file__).parent / "fixtures" / "sec" / "docs"
MANIFEST = json.loads((DOCS / "manifest.json").read_text(encoding="utf-8"))
CIK = MANIFEST["cik"]
TENK = MANIFEST["tenk"]
EIGHTK = MANIFEST["eightk"]
ARCHIVES = f"https://www.sec.gov/Archives/edgar/data/{CIK}/"
AAPL_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/320193/"
TICKERS = {
    "fields": ["cik", "name", "ticker", "exchange"],
    "data": [[CIK, "MICROSOFT CORP", "MSFT", "Nasdaq"], [320193, "Apple Inc.", "AAPL", "Nasdaq"]],
}


def _gz(name: str) -> bytes:
    return gzip.decompress((DOCS / name).read_bytes())


def _folder(acc: str) -> str:
    return ARCHIVES + acc.replace("-", "") + "/"


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    store = Cache(tmp_path / "cache.db")
    cache_mod.set_default_cache(store)
    monkeypatch.setenv("SEC_USER_AGENT", "Test Agent test@example.test")
    reset_limiters()
    yield store
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


_PARSED: dict[tuple[int, str], dict[str, str]] = {}
_REAL_EXTRACT = edgar_docs._extract_sections


@pytest.fixture(autouse=True)
def memo_parse(monkeypatch):
    """Parse the 10-K fixture once per session (edgartools takes a few seconds per parse)."""

    def memo(html, form):
        key = (hash(html), form)
        if key not in _PARSED:
            _PARSED[key] = _REAL_EXTRACT(html, form)
        return dict(_PARSED[key])

    monkeypatch.setattr(edgar_docs, "_extract_sections", memo)


@pytest.fixture
def sec():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        routes = {
            "tickers": mock.get(edgar.TICKERS_URL).mock(
                return_value=httpx.Response(200, json=TICKERS)
            ),
            "tenk_index": mock.get(
                _folder(TENK["accession"]) + TENK["accession"] + "-index.htm"
            ).mock(return_value=httpx.Response(200, content=_gz("msft_10k_index.htm.gz"))),
            "tenk_doc": mock.get(_folder(TENK["accession"]) + TENK["primary_document"]).mock(
                return_value=httpx.Response(200, content=_gz("msft_10k_primary.htm.gz"))
            ),
            "eightk_index": mock.get(
                _folder(EIGHTK["accession"]) + EIGHTK["accession"] + "-index.htm"
            ).mock(return_value=httpx.Response(200, content=_gz("msft_8k_index.htm.gz"))),
            # EDGAR answering an AAPL-folder URL with MSFT's index: the filer CIK must be checked.
            "aapl_tenk_index": mock.get(
                AAPL_ARCHIVES
                + TENK["accession"].replace("-", "")
                + "/"
                + TENK["accession"]
                + "-index.htm"
            ).mock(return_value=httpx.Response(200, content=_gz("msft_10k_index.htm.gz"))),
            "ex99": mock.get(_folder(EIGHTK["accession"]) + EIGHTK["ex99_1"]).mock(
                return_value=httpx.Response(200, content=_gz("msft_8k_ex99_1.htm.gz"))
            ),
        }
        yield routes


async def _all_chunks(fn, *args):
    first = await fn(*args)
    chunks = [first]
    while chunks[-1].next_cursor:
        chunks.append(await fn(*args, chunks[-1].next_cursor))
    return chunks


# ---------------------------------------------------------------- sections


async def test_risk_factors_extracted_with_sources(sec):
    r = await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A")
    assert r.text.strip()
    assert "RISK FACTORS" in r.text[:200].upper()
    assert r.source == "SEC EDGAR"
    assert r.source_url == _folder(TENK["accession"]) + TENK["primary_document"]
    assert r.as_of == date.fromisoformat(TENK["filing_date"])
    assert r.retrieved_at is not None
    assert r.form == "10-K" and r.section == "Item 1A" and r.cik == "0000789019"
    assert r.chunk_index == 0 and r.total_chunks >= 2


@pytest.mark.parametrize("alias", ["Item 1A", "item 1a", "Risk Factors", "risk_factors"])
async def test_section_aliases_resolve_to_1a(sec, alias):
    r = await edgar_docs.get_filing_section("MSFT", TENK["accession"].replace("-", ""), alias)
    assert r.section == "Item 1A"
    assert "RISK FACTORS" in r.text[:200].upper()


async def test_mdna_alias(sec):
    r = await edgar_docs.get_filing_section("MSFT", TENK["accession"], "MD&A")
    assert r.section == "Item 7"
    assert "MANAGEMENT" in r.text[:200].upper()


async def test_chunking_cursor_round_trip_covers_text(sec):
    chunks = await _all_chunks(edgar_docs.get_filing_section, "MSFT", TENK["accession"], "1A")
    total = chunks[0].total_chunks
    assert len(chunks) == total >= 2
    assert [c.chunk_index for c in chunks] == list(range(total))
    assert all(len(c.text) <= edgar_docs.CHUNK_CHARS for c in chunks)
    assert chunks[-1].next_cursor is None
    joined = "".join(c.text for c in chunks)
    assert len(joined) == chunks[0].total_chars > 50_000
    assert joined.rstrip().endswith(chunks[-1].text.rstrip())


def test_split_chunks_paragraph_boundaries_lossless():
    paras = [(f"p{i} ") * (i * 40 + 1) for i in range(60)]
    text = "\n\n".join(paras) + "\n" + "x" * 30_000
    chunks = edgar_docs.split_chunks(text, 5_000)
    assert "".join(chunks) == text
    assert all(len(c) <= 5_000 for c in chunks)
    # Chunks (except hard splits of an oversized paragraph) end on a paragraph break.
    assert chunks[0].endswith("\n\n")


async def test_cursor_for_other_section_is_bad_input(sec):
    r = await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A")
    with pytest.raises(BadInput):
        await edgar_docs.get_filing_section("MSFT", TENK["accession"], "7", r.next_cursor)
    with pytest.raises(BadInput):
        await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A", "garbage!")


async def test_cache_hit_on_second_call(sec):
    await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A")
    assert sec["tenk_doc"].call_count == 1
    r = await edgar_docs.get_filing_section("MSFT", TENK["accession"], "7")
    r2 = await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A")
    assert sec["tenk_doc"].call_count == 1
    assert sec["tenk_index"].call_count == 1
    assert r.text and r2.text


async def test_unknown_section_is_bad_input_listing_names(sec):
    with pytest.raises(BadInput) as exc:
        await edgar_docs.get_filing_section("MSFT", TENK["accession"], "Item 42")
    assert "1A" in str(exc.value) and "MD&A" in str(exc.value)
    assert sec["tenk_doc"].call_count == 0


async def test_section_on_8k_is_bad_input(sec):
    with pytest.raises(BadInput):
        await edgar_docs.get_filing_section("MSFT", EIGHTK["accession"], "1A")


async def test_bad_accession_is_bad_input():
    with pytest.raises(BadInput):
        await edgar_docs.get_filing_section("MSFT", "12345", "1A")


async def test_empty_section_is_not_found(sec, monkeypatch):
    real = edgar_docs._extract_sections

    def without_1c(html, form):
        out = real(html, form)
        out.pop("1C", None)
        return out

    monkeypatch.setattr(edgar_docs, "_extract_sections", without_1c)
    with pytest.raises(NotFound):
        await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1C")


async def test_accession_of_other_company_is_bad_input(sec):
    """A cached MSFT filing must not be served under AAPL; the index's filer CIK is checked."""
    await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A")
    aapl_index = sec["aapl_tenk_index"]
    with pytest.raises(BadInput) as exc:
        await edgar_docs.get_filing_section("AAPL", TENK["accession"], "1A")
    assert aapl_index.call_count == 1  # not answered from the MSFT cache entry
    assert "320193" in str(exc.value) or "AAPL" in str(exc.value)
    with pytest.raises(BadInput):
        await edgar_docs.get_filing_exhibit("AAPL", TENK["accession"], "EX-21.1")


async def test_empty_extraction_is_not_cached(sec, monkeypatch):
    good = edgar_docs._extract_sections
    monkeypatch.setattr(edgar_docs, "_extract_sections", lambda html, form: {})
    with pytest.raises(NotFound):
        await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A")
    monkeypatch.setattr(edgar_docs, "_extract_sections", good)
    r = await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A")
    assert r.text.strip()
    assert sec["tenk_doc"].call_count == 2


async def test_partial_extraction_cached_with_short_ttl(sec, env, monkeypatch):
    real = edgar_docs._extract_sections

    def without_1c(html, form):
        out = real(html, form)
        out.pop("1C", None)
        return out

    monkeypatch.setattr(edgar_docs, "_extract_sections", without_1c)
    with pytest.raises(NotFound):
        await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1C")
    key = f"{int(CIK):010d}|{TENK['accession']}|sections"
    assert not env.get(edgar_docs.NS, key).expired
    later = edgar_docs.utcnow() + timedelta(days=2)
    assert env.get(edgar_docs.NS, key, now=later).expired


async def test_expired_cache_entry_is_a_miss(env):
    past = edgar_docs.utcnow() - timedelta(days=3)
    env.set(edgar_docs.NS, "k", "v", 3600, past)
    assert await edgar_docs._cache_get("k") is None


# ---------------------------------------------------------------- exhibits


async def test_ex99_text(sec):
    r = await edgar_docs.get_filing_exhibit("MSFT", EIGHTK["accession"], "ex-99.1")
    assert "Revenue" in r.text
    assert "<td" not in r.text.lower()
    assert r.exhibit == "EX-99.1" and r.form == "8-K"
    assert r.source == "SEC EDGAR"
    assert r.source_url == _folder(EIGHTK["accession"]) + EIGHTK["ex99_1"]
    assert r.as_of == date.fromisoformat(EIGHTK["filing_date"])
    assert r.total_chunks >= 1


async def test_exhibit_cached(sec):
    await edgar_docs.get_filing_exhibit("MSFT", EIGHTK["accession"], "99.1")
    await edgar_docs.get_filing_exhibit("MSFT", EIGHTK["accession"], "EX-99.1")
    assert sec["ex99"].call_count == 1


async def test_missing_exhibit_not_found_lists_existing(sec):
    with pytest.raises(NotFound) as exc:
        await edgar_docs.get_filing_exhibit("MSFT", EIGHTK["accession"], "EX-10.1")
    assert "EX-99.1" in str(exc.value)


# ---------------------------------------------------------------- live


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("SEC_USER_AGENT"), reason="SEC_USER_AGENT not set")
async def test_live_msft_10k_risk_factors():
    r = await edgar_docs.get_filing_section("MSFT", TENK["accession"], "1A")
    assert "RISK FACTORS" in r.text[:200].upper()
