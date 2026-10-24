"""SEC EDGAR: ticker resolution and filing lists (read-only, public data).

Plain async helpers (`get_ticker_table`, `get_cik`, `get_company`, `get_submissions`,
`list_filings_data`) are reusable by other modules; `register(mcp)` exposes the tools.
All HTTP goes through `http.fetch("edgar", ...)`, which sets the User-Agent and rate-limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from market_research_mcp import http
from market_research_mcp.errors import BadInput, NotFound
from market_research_mcp.models import Sourced, SourcedList

__all__ = [
    "SOURCE",
    "TICKERS_URL",
    "CompanyRef",
    "Filing",
    "FilingList",
    "ResolvedTicker",
    "accession_url",
    "get_cik",
    "get_company",
    "get_submissions",
    "get_ticker_table",
    "list_filings",
    "list_filings_data",
    "normalize_ticker",
    "pad_cik",
    "register",
    "resolve_ticker",
    "submissions_url",
]

SOURCE = "SEC EDGAR"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions/"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"
MAX_LIMIT = 200


# ---------------------------------------------------------------- models


class ResolvedTicker(Sourced):
    cik: str
    name: str
    ticker: str
    exchange: str | None = None
    sic: str | None = None
    sic_description: str | None = None


class Filing(BaseModel):
    form: str
    filing_date: date
    report_date: date | None = None
    accession: str
    primary_document: str | None = None
    items: str | None = None
    url: str | None = None


class FilingList(SourcedList[Filing]):
    cik: str
    name: str
    ticker: str


@dataclass(frozen=True)
class CompanyRef:
    cik: str  # 10-digit zero-padded
    name: str
    ticker: str
    exchange: str | None


@dataclass(frozen=True)
class Submissions:
    data: dict[str, Any]
    url: str
    fetched_at: datetime
    stale: bool


# ---------------------------------------------------------------- helpers


def pad_cik(cik: int | str) -> str:
    """Zero-pad a CIK to 10 digits."""
    return str(int(str(cik).strip())).zfill(10)


def normalize_ticker(ticker: str) -> str:
    """Upper-case and use EDGAR's dash for share classes: 'brk.b' -> 'BRK-B'."""
    return ticker.strip().upper().replace(".", "-").replace("/", "-").replace(" ", "")


def submissions_url(cik: int | str) -> str:
    return f"{SUBMISSIONS_BASE}CIK{pad_cik(cik)}.json"


def accession_url(cik: int | str, accession: str, primary_document: str | None) -> str:
    """Archive URL of a filing's primary document (or its folder if no document)."""
    base = f"{ARCHIVES_BASE}{int(str(cik))}/{accession.replace('-', '')}/"
    return base + primary_document if primary_document else base


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _validate_since(since: str | None) -> date | None:
    if since is None or since == "":
        return None
    try:
        if len(since) != 10:
            raise ValueError
        return date.fromisoformat(since)
    except ValueError:
        raise BadInput(f"since must be a date in YYYY-MM-DD format, got {since!r}") from None


async def get_ticker_table() -> tuple[list[CompanyRef], http.FetchResult]:
    """All EDGAR companies with tickers (cached 7 days)."""
    res = await http.fetch("edgar", TICKERS_URL, cache_ns="tickers", cache_key="exchange")
    fields: list[str] = res.data["fields"]
    idx = {name: i for i, name in enumerate(fields)}
    rows = [
        CompanyRef(
            cik=pad_cik(row[idx["cik"]]),
            name=str(row[idx["name"]]),
            ticker=str(row[idx["ticker"]]),
            exchange=row[idx["exchange"]] if "exchange" in idx else None,
        )
        for row in res.data["data"]
    ]
    return rows, res


def _match(rows: list[CompanyRef], query: str) -> CompanyRef | None:
    q = query.strip()
    if not q:
        return None
    if q.isdigit():
        cik = pad_cik(q)
        for row in rows:
            if row.cik == cik:
                return row
    norm = normalize_ticker(q)
    for row in rows:
        if normalize_ticker(row.ticker) == norm:
            return row
    low = q.lower()
    for row in rows:  # file order is roughly by market cap, so the first hit is the likeliest
        if low in row.name.lower():
            return row
    return None


async def get_company(query: str) -> CompanyRef:
    """Find a company by ticker (BRK.B / BRK-B), CIK digits, or name substring."""
    rows, _ = await get_ticker_table()
    found = _match(rows, query)
    if found is None:
        raise NotFound(f"company matching {query.strip()!r}", SOURCE)
    return found


async def get_cik(ticker: str) -> str:
    """10-digit zero-padded CIK for a ticker (or name/CIK query)."""
    return (await get_company(ticker)).cik


async def get_submissions(cik: int | str) -> Submissions:
    """Submissions JSON for a CIK (cached 6 hours)."""
    url = submissions_url(cik)
    res = await http.fetch("edgar", url, cache_ns="submissions", cache_key=f"CIK{pad_cik(cik)}")
    return Submissions(res.data, url, res.fetched_at, res.stale)


async def _get_submissions_page(name: str) -> dict[str, Any]:
    res = await http.fetch("edgar", SUBMISSIONS_BASE + name, cache_ns="submissions", cache_key=name)
    return res.data


def _rows(block: dict[str, Any], cik: str) -> list[Filing]:
    acc = block.get("accessionNumber", [])
    out: list[Filing] = []
    for i, accession in enumerate(acc):

        def col(key: str, i: int = i) -> Any:
            values = block.get(key) or []
            return values[i] if i < len(values) else None

        filed = _parse_date(col("filingDate"))
        if filed is None:
            continue
        doc = col("primaryDocument") or None
        out.append(
            Filing(
                form=str(col("form") or ""),
                filing_date=filed,
                report_date=_parse_date(col("reportDate")),
                accession=accession,
                primary_document=doc,
                items=col("items") or None,
                url=accession_url(cik, accession, doc),
            )
        )
    return out


async def list_filings_data(
    cik: int | str,
    forms: list[str] | None = None,
    since: date | None = None,
    limit: int | None = None,
) -> tuple[list[Filing], bool, Submissions]:
    """Filings newest first, filtered by form and filing date >= since.

    Returns (filings, truncated, submissions). Older submission pages are fetched only when
    `since` reaches earlier than the `recent` block.
    """
    sub = await get_submissions(cik)
    cik10 = pad_cik(cik)
    filings_block = sub.data.get("filings", {})
    rows = _rows(filings_block.get("recent", {}), cik10)

    if since is not None:
        earliest = min((f.filing_date for f in rows), default=None)
        if earliest is None or earliest > since:
            for page in filings_block.get("files", []):
                page_to = _parse_date(page.get("filingTo"))
                if page_to is not None and page_to < since:
                    continue
                rows.extend(_rows(await _get_submissions_page(page["name"]), cik10))

    wanted = {f.strip().upper() for f in forms if f.strip()} if forms else None
    out = [
        f
        for f in rows
        if (wanted is None or f.form.upper() in wanted)
        and (since is None or f.filing_date >= since)
    ]
    out.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
    truncated = False
    if limit is not None and len(out) > limit:
        out, truncated = out[:limit], True
    return out, truncated, sub


# ---------------------------------------------------------------- tools


async def resolve_ticker(query: str) -> ResolvedTicker:
    """Resolve a ticker (e.g. MSFT, BRK.B) or company name to its SEC CIK, exchange and SIC
    industry code. Source: SEC EDGAR."""
    if not query or not query.strip():
        raise BadInput("query must not be empty")
    rows, tickers_res = await get_ticker_table()
    company = _match(rows, query)
    if company is None:
        raise NotFound(f"company matching {query.strip()!r}", SOURCE)
    sub = await get_submissions(company.cik)
    sic = sub.data.get("sic")
    return ResolvedTicker(
        source=SOURCE,
        source_url=TICKERS_URL,
        retrieved_at=tickers_res.fetched_at,
        as_of=tickers_res.fetched_at.date(),
        stale=tickers_res.stale or sub.stale,
        notes=[f"SIC code from {sub.url}"],
        cik=company.cik,
        name=company.name,
        ticker=company.ticker,
        exchange=company.exchange,
        sic=str(sic) if sic else None,
        sic_description=sub.data.get("sicDescription") or None,
    )


async def list_filings(
    ticker: str,
    forms: list[str] | None = None,
    since: str | None = None,
    limit: int = 20,
) -> FilingList:
    """List a company's SEC filings, newest first. Filter by form types (e.g. ["10-K",
    "10-Q", "8-K"]) and filing date `since` (YYYY-MM-DD). Source: SEC EDGAR."""
    since_date = _validate_since(since)
    if limit < 1 or limit > MAX_LIMIT:
        raise BadInput(f"limit must be between 1 and {MAX_LIMIT}")
    company = await get_company(ticker)
    items, truncated, sub = await list_filings_data(company.cik, forms, since_date, limit)
    latest = max((f.filing_date for f in items), default=None)
    return FilingList(
        source=SOURCE,
        source_url=sub.url,
        retrieved_at=sub.fetched_at,
        as_of=latest,
        stale=sub.stale,
        items=items,
        limit=limit,
        truncated=truncated,
        cik=company.cik,
        name=str(sub.data.get("name") or company.name),
        ticker=company.ticker,
    )


def register(mcp: FastMCP) -> None:
    mcp.tool()(resolve_ticker)
    mcp.tool()(list_filings)
