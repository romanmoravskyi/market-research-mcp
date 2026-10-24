"""Tests for sources/insiders.py (SEC Form 4). No network: respx serves recorded fixtures.

Fixtures: tests/fixtures/sec/form4/ holds real MSFT Form 4 XML + index.json (gzip), recorded by
scripts/record_form4_fixtures.py. The derivative (code M) case uses a small synthetic XML below
because the recorded MSFT filings contain no derivative transactions.
"""

import gzip
import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.cache import Cache
from market_research_mcp.errors import BadInput
from market_research_mcp.ratelimit import reset_limiters
from market_research_mcp.sources import edgar, insiders

SEC = Path(__file__).parent / "fixtures" / "sec"
FORM4 = SEC / "form4"
SUB = "https://data.sec.gov/submissions/"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/789019/"
MSFT_CIK = "0000789019"

# Filings dated on/after 2026-09-16 in the MSFT submissions fixture; all are recorded.
RECENT7 = [
    "0000789019-26-000228",  # F, 2026-10-15
    "0000789019-26-000226",  # S x3, 2026-10-14, aff10b5One=1
    "0000789019-26-000224",  # A, 2026-09-17
    "0000789019-26-000223",
    "0000789019-26-000222",
    "0000789019-26-000221",
    "0000789019-26-000220",
]

DERIVATIVE_XML = b"""<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0000000001</rptOwnerCik>
      <rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector><isOfficer>0</isOfficer>
      <isTenPercentOwner>1</isTenPercentOwner><isOther>0</isOther>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-01-05</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>10.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>1100</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Stock Option (right to buy)</value></securityTitle>
      <transactionDate><value>2026-01-06</value></transactionDate>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>0</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
      <footnoteId id="F2"/>
    </derivativeTransaction>
  </derivativeTable>
  <footnotes>
    <footnote id="F1">Not applicable.</footnote>
    <footnote id="F2">Exercised under a Rule 10b5-1 trading plan adopted May 1, 2025.</footnote>
  </footnotes>
</ownershipDocument>
"""


def _read(path_json: Path) -> bytes:
    gz = path_json.with_name(path_json.name + ".gz")
    if path_json.exists():
        return path_json.read_bytes()
    return gzip.decompress(gz.read_bytes())


def submissions_bytes() -> bytes:
    return _read(SEC / f"CIK{MSFT_CIK}.json")


@pytest.fixture(autouse=True)
async def env(tmp_path, monkeypatch):
    cache_mod.set_default_cache(Cache(tmp_path / "cache.db"))
    reset_limiters()
    monkeypatch.setenv("SEC_USER_AGENT", "Test Agent test@example.test")
    yield
    await http.aclose()
    cache_mod.set_default_cache(None)
    reset_limiters()


@pytest.fixture
def sec():
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        mock.get(edgar.TICKERS_URL).mock(
            return_value=httpx.Response(200, content=_read(SEC / "company_tickers_exchange.json"))
        )
        mock.get(SUB + f"CIK{MSFT_CIK}.json").mock(
            return_value=httpx.Response(200, content=submissions_bytes())
        )
        routes = {}
        for path in FORM4.glob("*.gz"):
            m = re.match(r"(\d{10}-\d{2}-\d{6})-(.+)\.gz$", path.name)
            assert m, path.name
            accession, doc = m.groups()
            url = f"{ARCHIVE}{accession.replace('-', '')}/{doc}"
            routes[(accession, doc)] = mock.get(url).mock(
                return_value=httpx.Response(200, content=gzip.decompress(path.read_bytes()))
            )
        routes["other"] = mock.get(url__startswith=ARCHIVE).mock(return_value=httpx.Response(404))
        yield routes


# ---------------------------------------------------------------- parser


def test_parse_real_sale_filing():
    xml = gzip.decompress((FORM4 / "0000789019-26-000226-form4.xml.gz").read_bytes())
    rows = insiders.parse_form4(xml, "0000789019-26-000226", date(2026, 10, 14), "u")
    assert len(rows) == 3
    r = rows[0]
    assert r.insider_name == "Hood Amy"
    assert r.role == "Officer: EVP, Chief Financial Officer"
    assert r.transaction_date == date(2026, 10, 13)
    assert r.code == "S" and r.is_open_market is True
    assert "sale" in r.code_description.lower()
    assert r.acquired_disposed == "D"
    assert r.shares == 3387 and r.price_per_share == pytest.approx(544.8001)
    assert r.shares_owned_after == pytest.approx(559314.427)
    assert r.value == pytest.approx(3387 * 544.8001)
    assert r.derivative is False
    assert r.rule_10b5_1 is True  # aff10b5One = 1
    assert r.accession == "0000789019-26-000226" and r.filing_url == "u"
    assert r.filing_date == date(2026, 10, 14)


def test_parse_real_tax_withholding_not_open_market():
    xml = gzip.decompress((FORM4 / "0000789019-26-000228-form4.xml.gz").read_bytes())
    rows = insiders.parse_form4(xml, "0000789019-26-000228", date(2026, 10, 15), "u")
    assert rows and all(r.code == "F" for r in rows)
    assert all(r.is_open_market is False for r in rows)
    assert "tax" in rows[0].code_description.lower()


def test_parse_award_aff10b5one_zero_is_false():
    xml = gzip.decompress((FORM4 / "0000789019-26-000224-form4.xml.gz").read_bytes())
    rows = insiders.parse_form4(xml, "0000789019-26-000224", date(2026, 9, 17), "u")
    assert rows[0].code == "A" and rows[0].is_open_market is False
    assert rows[0].rule_10b5_1 is False
    assert rows[0].role == "Officer: Vice Chair and President"


def test_parse_synthetic_derivative_and_purchase():
    rows = insiders.parse_form4(DERIVATIVE_XML, "acc", date(2026, 1, 7), "u")
    assert [r.code for r in rows] == ["P", "M"]
    p, m = rows
    assert p.derivative is False and p.is_open_market is True and p.acquired_disposed == "A"
    assert p.role == "Director; 10% owner"
    assert p.rule_10b5_1 is None  # no element, no footnote on this row
    assert m.derivative is True and m.is_open_market is False
    assert m.security_title == "Stock Option (right to buy)"
    assert m.price_per_share is None and m.value is None
    assert m.shares == 500 and m.shares_owned_after == 0
    assert m.rule_10b5_1 is True  # from footnote text
    assert "derivative" in m.code_description.lower()


def test_summary_math():
    rows = insiders.parse_form4(DERIVATIVE_XML, "acc", date(2026, 1, 7), "u")
    s = insiders.summarize(rows)
    assert s.open_market_purchases.count == 1
    assert s.open_market_purchases.shares == 100
    assert s.open_market_purchases.value == pytest.approx(1050.0)
    assert s.open_market_sales.count == 0 and s.open_market_sales.value == 0
    assert "open-market purchases" in s.open_market_purchases.label
    assert [(c.code, c.count) for c in s.not_open_market_trades] == [("M", 1)]
    assert "not open-market" in s.not_open_market_label


# ---------------------------------------------------------------- tool


async def test_tool_since_filter_and_source(sec):
    res = await insiders.get_insider_transactions("MSFT", since="2026-09-16")
    accs = {r.accession for r in res.items}
    assert accs == set(RECENT7)
    assert res.filings_fetched == 7 and res.filings_capped is False
    assert res.source == "SEC EDGAR Form 4"
    assert "browse-edgar" in res.source_url and "type=4" in res.source_url
    assert MSFT_CIK in res.source_url
    assert res.as_of == date(2026, 10, 15)
    assert res.retrieved_at.tzinfo is not None
    assert res.cik == MSFT_CIK and res.ticker == "MSFT"
    assert all(r.filing_date >= date(2026, 9, 16) for r in res.items)
    # newest first
    keys = [r.transaction_date or r.filing_date for r in res.items]
    assert keys == sorted(keys, reverse=True)
    assert res.items[0].filing_url.startswith(ARCHIVE + "000078901926000228/")
    insiders.InsiderTransactions.model_validate(res.model_dump(mode="json"))


async def test_tool_summary_labels(sec):
    res = await insiders.get_insider_transactions("MSFT", since="2026-10-14")
    assert {r.accession for r in res.items} == set(RECENT7[:2])
    s = res.summary
    sales = [r for r in res.items if r.code == "S"]
    assert s.open_market_sales.count == len(sales) == 3
    assert s.open_market_sales.shares == pytest.approx(3387 + 2927 + 991)
    assert s.open_market_sales.value == pytest.approx(sum(r.value for r in sales))
    assert s.open_market_purchases.count == 0
    codes = {c.code for c in s.not_open_market_trades}
    assert codes == {"F"}
    assert all(not r.is_open_market for r in res.items if r.code == "F")


async def test_tool_limit_truncated(sec):
    res = await insiders.get_insider_transactions("MSFT", since="2026-09-16", limit=2)
    assert len(res.items) == 2 and res.truncated is True and res.limit == 2
    # summary covers all fetched rows, not just the returned ones
    assert res.summary.open_market_sales.count == 3
    full = await insiders.get_insider_transactions("MSFT", since="2026-09-16", limit=50)
    assert full.truncated is False


async def test_tool_default_since_and_cap(sec, monkeypatch):
    monkeypatch.setattr(insiders, "MAX_FILINGS", 3)
    res = await insiders.get_insider_transactions("MSFT")
    assert res.filings_capped is True and res.filings_fetched == 3
    assert {r.accession for r in res.items} == set(RECENT7[:3])
    assert any("capped" in n.lower() or "first 3" in n for n in res.notes)


async def test_tool_skips_missing_filing_with_note(sec, monkeypatch):
    monkeypatch.setattr(insiders, "MAX_FILINGS", 9)  # 213 is not recorded -> 404
    res = await insiders.get_insider_transactions("MSFT", since="2026-09-15")
    accs = {r.accession for r in res.items}
    assert "0000789019-26-000214" in accs
    assert "0000789019-26-000213" not in accs
    assert any("0000789019-26-000213" in n for n in res.notes)


async def test_xml_name_from_index_when_primary_document_missing(sec):
    xml = await insiders.fetch_form4_xml(MSFT_CIK, "0000789019-26-000228", None)
    assert b"<ownershipDocument>" in xml
    assert sec[("0000789019-26-000228", "index.json")].called


async def test_bad_inputs(sec):
    with pytest.raises(BadInput):
        await insiders.get_insider_transactions("MSFT", since="2026/01/01")
    with pytest.raises(BadInput):
        await insiders.get_insider_transactions("MSFT", limit=0)


# ---------------------------------------------------------------- review fixes (PR #4)


def synth_xml(
    *,
    issuer_cik: str = MSFT_CIK,
    owner_cik: str = "0000000001",
    owner_name: str = "Doe Jane",
    doc_type: str = "4",
    original_date: str | None = None,
    rows: list[tuple[bool, str, float, float]] = (),
) -> bytes:
    """Synthetic Form 4 XML. rows: (derivative, code, shares, price)."""

    def tx(tag: str, code: str, shares: float, price: float) -> str:
        return (
            f"<{tag}><securityTitle><value>Common Stock</value></securityTitle>"
            "<transactionDate><value>2026-10-01</value></transactionDate>"
            f"<transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>"
            f"<transactionAmounts><transactionShares><value>{shares}</value></transactionShares>"
            f"<transactionPricePerShare><value>{price}</value></transactionPricePerShare>"
            "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>"
            f"</transactionAmounts></{tag}>"
        )

    nd = "".join(tx("nonDerivativeTransaction", c, s, p) for d, c, s, p in rows if not d)
    dv = "".join(tx("derivativeTransaction", c, s, p) for d, c, s, p in rows if d)
    orig = f"<dateOfOriginalSubmission>{original_date}</dateOfOriginalSubmission>"
    return (
        '<?xml version="1.0"?><ownershipDocument>'
        f"<documentType>{doc_type}</documentType>"
        f"{orig if original_date else ''}"
        f"<issuer><issuerCik>{issuer_cik}</issuerCik></issuer>"
        f"<reportingOwner><reportingOwnerId><rptOwnerCik>{owner_cik}</rptOwnerCik>"
        f"<rptOwnerName>{owner_name}</rptOwnerName></reportingOwnerId>"
        "<reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>"
        "</reportingOwner>"
        f"<nonDerivativeTable>{nd}</nonDerivativeTable>"
        f"<derivativeTable>{dv}</derivativeTable>"
        "</ownershipDocument>"
    ).encode()


@pytest.fixture
def synthetic(sec, monkeypatch):
    """Serve synthetic filings: `docs` maps accession -> (form, filing_date, xml)."""
    docs: dict[str, tuple[str, date, bytes]] = {}
    real_list = insiders.list_filings_data

    async def fake_list(cik, forms=None, since=None, limit=None, **kw):
        _, truncated, sub = await real_list(cik, forms=["4"], since=since)
        wanted = {f.upper() for f in forms} if forms else None
        out = [
            edgar.Filing(form=form, filing_date=fd, accession=acc, primary_document="x.xml")
            for acc, (form, fd, _) in docs.items()
            if wanted is None or form.upper() in wanted
        ]
        out.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
        return out, truncated, sub

    async def fake_fetch(cik, accession, primary_document):
        return docs[accession][2]

    monkeypatch.setattr(insiders, "list_filings_data", fake_list)
    monkeypatch.setattr(insiders, "fetch_form4_xml", fake_fetch)
    return docs


async def test_skips_filings_where_company_is_reporting_owner_of_other_issuer(synthetic):
    # e.g. BRK-B buying OXY: the company is the reporting owner, issuer is someone else.
    synthetic["acc-own"] = ("4", date(2026, 10, 2), synth_xml(rows=[(False, "P", 100, 10)]))
    synthetic["acc-other"] = (
        "4",
        date(2026, 10, 3),
        synth_xml(
            issuer_cik="0000797468",
            owner_cik=MSFT_CIK,
            owner_name="MICROSOFT CORP",
            rows=[(False, "P", 1_000_000, 60)],
        ),
    )
    res = await insiders.get_insider_transactions("MSFT", since="2026-01-01")
    assert {r.accession for r in res.items} == {"acc-own"}
    assert res.summary.open_market_purchases.count == 1
    assert res.summary.open_market_purchases.value == pytest.approx(1000.0)
    assert res.filings_fetched == 1
    assert any("acc-other" in n and "issuer" in n.lower() for n in res.notes)


async def test_form4_amendment_included_and_supersedes_original(synthetic):
    synthetic["acc-orig"] = ("4", date(2026, 10, 2), synth_xml(rows=[(False, "P", 100, 10)]))
    synthetic["acc-amend"] = (
        "4/A",
        date(2026, 10, 5),
        synth_xml(doc_type="4/A", original_date="2026-10-02", rows=[(False, "P", 150, 10)]),
    )
    # unrelated original by another insider on the same day stays
    synthetic["acc-other-owner"] = (
        "4",
        date(2026, 10, 2),
        synth_xml(owner_cik="0000000002", rows=[(False, "S", 10, 20)]),
    )
    res = await insiders.get_insider_transactions("MSFT", since="2026-01-01")
    accs = {r.accession for r in res.items}
    assert accs == {"acc-amend", "acc-other-owner"}
    amended = [r for r in res.items if r.accession == "acc-amend"]
    assert amended and all(r.amended for r in amended)
    assert all(not r.amended for r in res.items if r.accession != "acc-amend")
    assert res.summary.open_market_purchases.shares == 150  # not 250: no double count
    assert res.summary.open_market_sales.count == 1
    assert any("acc-orig" in n and "acc-amend" in n for n in res.notes)


async def test_form4_amendment_without_original_in_window_is_kept(synthetic):
    synthetic["acc-amend"] = (
        "4/A",
        date(2026, 10, 5),
        synth_xml(doc_type="4/A", original_date="2025-01-02", rows=[(False, "S", 5, 20)]),
    )
    res = await insiders.get_insider_transactions("MSFT", since="2026-01-01")
    assert [r.accession for r in res.items] == ["acc-amend"]
    assert res.items[0].amended is True
    assert res.summary.open_market_sales.count == 1


def test_summary_excludes_derivative_open_market_rows():
    xml = synth_xml(rows=[(False, "S", 100, 50), (True, "S", 10_000, 3.5), (True, "P", 200, 1)])
    rows = insiders.parse_form4(xml, "acc", date(2026, 10, 2), "u")
    deriv = [r for r in rows if r.derivative]
    assert len(deriv) == 2  # still listed
    assert all(r.is_open_market is False for r in deriv)  # marked: not common-stock trades
    s = insiders.summarize(rows)
    assert s.open_market_sales.count == 1
    assert s.open_market_sales.shares == 100
    assert s.open_market_sales.value == pytest.approx(5000.0)
    assert s.open_market_purchases.count == 0
    assert sorted((c.code, c.count) for c in s.derivative_trades) == [("P", 1), ("S", 1)]
    assert all(c.code not in {"P", "S"} for c in s.not_open_market_trades)


async def test_form4_fetches_run_concurrently_with_bound(synthetic, monkeypatch):
    import asyncio

    for i in range(12):
        synthetic[f"acc-{i:02d}"] = (
            "4",
            date(2026, 10, 1 + i),
            synth_xml(rows=[(False, "P", 1, 1)]),
        )
    in_flight = 0
    peak = 0

    async def slow_fetch(cik, accession, primary_document):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return synthetic[accession][2]

    monkeypatch.setattr(insiders, "fetch_form4_xml", slow_fetch)
    res = await insiders.get_insider_transactions("MSFT", since="2026-01-01")
    assert res.filings_fetched == 12
    assert 1 < peak <= insiders.FETCH_CONCURRENCY


def test_source_has_no_order_capability():
    text = Path(insiders.__file__).read_text(encoding="utf-8").lower()
    for word in ("place_order", "buy recommendation", "price target"):
        assert word not in text


# ---------------------------------------------------------------- live


@pytest.mark.live
async def test_live_msft_insiders():
    import os

    from dotenv import load_dotenv

    load_dotenv(override=True)  # replace the placeholder set by the env fixture
    if not os.environ.get("SEC_USER_AGENT"):
        pytest.skip("SEC_USER_AGENT not set")
    res = await insiders.get_insider_transactions("MSFT", limit=10)
    assert res.source == "SEC EDGAR Form 4"
    assert res.filings_fetched >= 1
    json.dumps(res.model_dump(mode="json"))


async def test_capped_result_states_the_window_the_summary_covers(sec, monkeypatch):
    # A capped call fetches only the newest filings, so "since" overstates coverage. The
    # response must say from which filing date the summary is complete.
    monkeypatch.setattr(insiders, "MAX_FILINGS", 3)
    res = await insiders.get_insider_transactions("MSFT")
    oldest = min(r.filing_date for r in res.items)
    assert res.covered_since == oldest
    assert res.covered_since > res.since
    assert any(f"covers filings from {oldest.isoformat()}" in n for n in res.notes)


async def test_uncapped_result_covers_since(sec):
    res = await insiders.get_insider_transactions("MSFT", since="2026-09-16")
    assert res.covered_since == res.since == date(2026, 9, 16)
