"""Insider transactions from SEC Form 4 filings (read-only, public data).

Lists a company's Form 4 filings from EDGAR submissions, fetches each filing's XML (cached
forever in `filing_docs`) and returns one row per reported transaction. Only codes P and S
are labelled open-market trades; every other code (awards, tax withholding, option
exercises, gifts, ...) is reported as "not open-market".
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from market_research_mcp import http
from market_research_mcp.errors import BadInput, NotFound
from market_research_mcp.models import SourcedList, utcnow
from market_research_mcp.sources.edgar import (
    ARCHIVES_BASE,
    _validate_since,
    get_company,
    list_filings_data,
    pad_cik,
)

__all__ = [
    "CODE_DESCRIPTIONS",
    "MAX_FILINGS",
    "SOURCE",
    "CodeCount",
    "InsiderSummary",
    "InsiderTransaction",
    "InsiderTransactions",
    "OpenMarketTotals",
    "fetch_form4_xml",
    "get_insider_transactions",
    "parse_form4",
    "register",
    "summarize",
]

SOURCE = "SEC EDGAR Form 4"
MAX_FILINGS = 40  # Form 4 filings fetched per call
MAX_LIMIT = 500
DEFAULT_LOOKBACK_DAYS = 365
OPEN_MARKET_CODES = {"P", "S"}

# SEC Form 4 General Instructions, item 8 (transaction codes).
CODE_DESCRIPTIONS: dict[str, str] = {
    "P": "Open market or private purchase",
    "S": "Open market or private sale",
    "V": "Transaction voluntarily reported earlier than required",
    "A": "Grant, award or other acquisition from the issuer",
    "D": "Disposition to the issuer",
    "F": "Payment of exercise price or tax liability by delivering or withholding securities",
    "I": "Discretionary transaction",
    "M": "Exercise or conversion of derivative security exempted under Rule 16b-3",
    "C": "Conversion of derivative security",
    "E": "Expiration of short derivative position",
    "H": "Expiration (or cancellation) of long derivative position with value received",
    "O": "Exercise of out-of-the-money derivative security",
    "X": "Exercise of in-the-money or at-the-money derivative security",
    "G": "Bona fide gift",
    "L": "Small acquisition under Rule 16a-6",
    "W": "Acquisition or disposition by will or the laws of descent and distribution",
    "Z": "Deposit into or withdrawal from voting trust",
    "J": "Other acquisition or disposition (see footnotes)",
    "K": "Transaction in equity swap or similar instrument",
    "U": "Disposition due to tender of shares in a change of control",
}

PURCHASES_LABEL = "open-market purchases (code P)"
SALES_LABEL = "open-market sales (code S)"
NOT_OPEN_MARKET_LABEL = (
    "not open-market trades (awards, tax withholding, option exercises, gifts and other codes)"
)


# ---------------------------------------------------------------- models


class InsiderTransaction(BaseModel):
    insider_name: str
    insider_cik: str | None = None
    role: str
    security_title: str | None = None
    transaction_date: date | None
    filing_date: date
    code: str
    code_description: str
    is_open_market: bool
    acquired_disposed: str | None
    shares: float | None
    price_per_share: float | None
    value: float | None  # shares * price_per_share when both are reported
    shares_owned_after: float | None
    derivative: bool
    rule_10b5_1: bool | None
    accession: str
    filing_url: str


class OpenMarketTotals(BaseModel):
    label: str
    count: int = 0
    shares: float = 0.0
    value: float = 0.0  # rows without a reported price add shares but no value


class CodeCount(BaseModel):
    code: str
    description: str
    count: int


class InsiderSummary(BaseModel):
    open_market_purchases: OpenMarketTotals
    open_market_sales: OpenMarketTotals
    not_open_market_label: str = NOT_OPEN_MARKET_LABEL
    not_open_market_trades: list[CodeCount]


class InsiderTransactions(SourcedList[InsiderTransaction]):
    cik: str
    name: str
    ticker: str
    since: date
    filings_fetched: int
    filings_capped: bool
    summary: InsiderSummary


# ---------------------------------------------------------------- parsing


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _num(el: ET.Element | None, path: str) -> float | None:
    value = _text(el, path)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    low = value.lower()
    if low in {"1", "true"}:
        return True
    if low in {"0", "false"}:
        return False
    return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _owners(root: ET.Element) -> tuple[str, str | None, str]:
    names: list[str] = []
    ciks: list[str] = []
    roles: list[str] = []
    for owner in root.findall("reportingOwner"):
        name = _text(owner, "reportingOwnerId/rptOwnerName")
        if name:
            names.append(name)
        cik = _text(owner, "reportingOwnerId/rptOwnerCik")
        if cik:
            ciks.append(cik)
        rel = owner.find("reportingOwnerRelationship")
        parts: list[str] = []
        if _flag(_text(rel, "isDirector")):
            parts.append("Director")
        if _flag(_text(rel, "isOfficer")):
            title = _text(rel, "officerTitle")
            parts.append(f"Officer: {title}" if title else "Officer")
        if _flag(_text(rel, "isTenPercentOwner")):
            parts.append("10% owner")
        if _flag(_text(rel, "isOther")):
            other = _text(rel, "otherText")
            parts.append(f"Other: {other}" if other else "Other")
        if parts:
            roles.append("; ".join(parts))
    return (
        "; ".join(names) or "Unknown",
        "; ".join(ciks) or None,
        " | ".join(dict.fromkeys(roles)) or "Not stated",
    )


def _mentions_10b5_1(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower().replace("‑", "-").replace("–", "-")
    return "10b5-1" in low or "10b-5-1" in low


def parse_form4(
    xml: bytes | str, accession: str, filing_date: date, filing_url: str
) -> list[InsiderTransaction]:
    """One row per non-derivative and derivative transaction in a Form 4 XML document."""
    root = ET.fromstring(xml)
    name, owner_cik, role = _owners(root)
    filing_flag = _flag(_text(root, "aff10b5One"))
    footnotes = {fn.get("id"): "".join(fn.itertext()) for fn in root.findall("footnotes/footnote")}
    remarks_10b5_1 = _mentions_10b5_1(_text(root, "remarks"))

    rows: list[InsiderTransaction] = []
    tables = (
        ("nonDerivativeTable/nonDerivativeTransaction", False),
        ("derivativeTable/derivativeTransaction", True),
    )
    for path, derivative in tables:
        for tx in root.findall(path):
            code = (_text(tx, "transactionCoding/transactionCode") or "").upper()
            shares = _num(tx, "transactionAmounts/transactionShares/value")
            price = _num(tx, "transactionAmounts/transactionPricePerShare/value")
            if filing_flag is not None:
                rule = filing_flag
            else:
                ids = {f.get("id") for f in tx.iter("footnoteId")}
                notes_hit = any(_mentions_10b5_1(footnotes.get(i)) for i in ids)
                rule = True if (notes_hit or remarks_10b5_1) else None
            rows.append(
                InsiderTransaction(
                    insider_name=name,
                    insider_cik=owner_cik,
                    role=role,
                    security_title=_text(tx, "securityTitle/value"),
                    transaction_date=_date(_text(tx, "transactionDate/value")),
                    filing_date=filing_date,
                    code=code,
                    code_description=CODE_DESCRIPTIONS.get(code, "Unknown transaction code"),
                    is_open_market=code in OPEN_MARKET_CODES,
                    acquired_disposed=_text(
                        tx, "transactionAmounts/transactionAcquiredDisposedCode/value"
                    ),
                    shares=shares,
                    price_per_share=price,
                    value=shares * price if shares is not None and price is not None else None,
                    shares_owned_after=_num(
                        tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"
                    ),
                    derivative=derivative,
                    rule_10b5_1=rule,
                    accession=accession,
                    filing_url=filing_url,
                )
            )
    return rows


def summarize(rows: list[InsiderTransaction]) -> InsiderSummary:
    """Totals for open-market P and S rows; counts per code for everything else."""
    buys = OpenMarketTotals(label=PURCHASES_LABEL)
    sells = OpenMarketTotals(label=SALES_LABEL)
    others: Counter[str] = Counter()
    for r in rows:
        if r.code in OPEN_MARKET_CODES:
            t = buys if r.code == "P" else sells
            t.count += 1
            t.shares += r.shares or 0.0
            t.value += r.value or 0.0
        else:
            others[r.code] += 1
    return InsiderSummary(
        open_market_purchases=buys,
        open_market_sales=sells,
        not_open_market_trades=[
            CodeCount(code=c, description=CODE_DESCRIPTIONS.get(c, "Unknown"), count=n)
            for c, n in sorted(others.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    )


# ---------------------------------------------------------------- fetching


def _folder(cik: str, accession: str) -> str:
    return f"{ARCHIVES_BASE}{int(cik)}/{accession.replace('-', '')}/"


async def _fetch_doc(folder: str, accession: str, name: str) -> bytes:
    res = await http.fetch(
        "edgar",
        folder + name,
        cache_ns="filing_docs",
        cache_key=f"{accession}/{name}",
        parse="bytes",
    )
    return res.data


async def fetch_form4_xml(cik: str, accession: str, primary_document: str | None) -> bytes:
    """Raw Form 4 XML. Uses the primary document name (minus the xsl rendering folder) when it
    is an XML file, else finds the XML in the filing's index.json."""
    folder = _folder(cik, accession)
    if primary_document and primary_document.lower().endswith(".xml"):
        try:
            return await _fetch_doc(folder, accession, primary_document.rsplit("/", 1)[-1])
        except NotFound:
            pass
    res = await http.fetch(
        "edgar",
        folder + "index.json",
        cache_ns="filing_docs",
        cache_key=f"{accession}/index.json",
    )
    items = res.data.get("directory", {}).get("item", [])
    names = [str(i.get("name", "")) for i in items]
    xml_name = next((n for n in names if n.lower().endswith(".xml")), None)
    if xml_name is None:
        raise NotFound(f"Form 4 XML in filing {accession}", SOURCE)
    return await _fetch_doc(folder, accession, xml_name)


def browse_url(cik: str) -> str:
    return (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={pad_cik(cik)}&type=4&dateb=&owner=include&count=40"
    )


# ---------------------------------------------------------------- tool


async def get_insider_transactions(
    ticker: str, since: str | None = None, limit: int = 50
) -> InsiderTransactions:
    """Insider transactions reported on SEC Form 4, newest first: insider, role, date,
    transaction code, shares, price, holdings after, derivative flag and Rule 10b5-1 flag.
    Only codes P and S count as open-market purchases/sales; awards (A), tax withholding (F),
    option exercises (M) and other codes are labelled not open-market. `since` is a filing
    date (YYYY-MM-DD, default 12 months ago). Source: SEC EDGAR Form 4."""
    since_date = _validate_since(since)
    if limit < 1 or limit > MAX_LIMIT:
        raise BadInput(f"limit must be between 1 and {MAX_LIMIT}")
    if since_date is None:
        since_date = utcnow().date() - timedelta(days=DEFAULT_LOOKBACK_DAYS)

    company = await get_company(ticker)
    filings, _, sub = await list_filings_data(company.cik, forms=["4"], since=since_date)
    capped = len(filings) > MAX_FILINGS
    total_filings = len(filings)
    filings = filings[:MAX_FILINGS]

    notes: list[str] = [
        "Codes P and S are open-market or private purchases/sales as reported; all other "
        "codes are not open-market trades.",
        "Summary covers all transactions in the fetched filings, before `limit` is applied.",
    ]
    if capped:
        notes.append(
            f"Filings capped: fetched the newest {MAX_FILINGS} of {total_filings} Form 4 "
            f"filings since {since_date.isoformat()}. Use a later `since` for full coverage."
        )

    rows: list[InsiderTransaction] = []
    fetched = 0
    stale = sub.stale
    for f in filings:
        try:
            xml = await fetch_form4_xml(company.cik, f.accession, f.primary_document)
        except NotFound:
            notes.append(f"Skipped filing {f.accession}: Form 4 XML not found.")
            continue
        url = _folder(company.cik, f.accession)
        try:
            rows.extend(parse_form4(xml, f.accession, f.filing_date, url))
        except ET.ParseError:
            notes.append(f"Skipped filing {f.accession}: XML could not be parsed.")
            continue
        fetched += 1

    rows.sort(
        key=lambda r: (r.transaction_date or r.filing_date, r.filing_date, r.accession),
        reverse=True,
    )
    summary = summarize(rows)
    truncated = len(rows) > limit
    newest = max((f.filing_date for f in filings), default=None)
    return InsiderTransactions(
        source=SOURCE,
        source_url=browse_url(company.cik),
        retrieved_at=sub.fetched_at,
        as_of=newest,
        stale=stale,
        notes=notes,
        items=rows[:limit],
        limit=limit,
        truncated=truncated,
        cik=company.cik,
        name=str(sub.data.get("name") or company.name),
        ticker=company.ticker,
        since=since_date,
        filings_fetched=fetched,
        filings_capped=capped,
        summary=summary,
    )


def register(mcp: FastMCP) -> None:
    mcp.tool()(get_insider_transactions)
