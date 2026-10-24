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
from market_research_mcp.tickers import canonical_ticker, normalize_ticker

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
MAX_EXTRA_PAGES = 5  # older submission pages fetched to satisfy a form filter
MAX_SUGGESTIONS = 3


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


def _looks_like_name(q: str) -> bool:
    """A company-name query: has a space, or is longer than 5 chars and not all caps.

    Short or all-caps single words ("MSF", "AMZ", "ZZZZNOTREAL") are treated as tickers, so a
    typo never silently resolves to whichever company name happens to contain it.
    """
    return " " in q or (len(q) > 5 and not q.isupper())


def _match(rows: list[CompanyRef], query: str) -> CompanyRef | None:
    q = query.strip()
    if not q:
        return None
    if q.isdigit():
        cik = pad_cik(q)
        for row in rows:
            if row.cik == cik:
                return row
    norm = canonical_ticker(q)
    for row in rows:
        if canonical_ticker(row.ticker) == norm:
            return row
    if _looks_like_name(q):
        low = q.lower()
        for row in rows:  # file order is roughly by market cap, so the first hit is the likeliest
            if low in row.name.lower():
                return row
    return None


def _suggest(rows: list[CompanyRef], query: str) -> list[str]:
    """Up to 3 likely companies: tickers starting with the query, then name hits."""
    norm = canonical_ticker(query)
    low = query.strip().lower()
    if not norm:
        return []
    picks = [r for r in rows if canonical_ticker(r.ticker).startswith(norm)]
    if len(picks) < MAX_SUGGESTIONS:
        picks += [r for r in rows if low in r.name.lower()]
    out: list[str] = []
    for r in picks:
        label = f"{r.ticker} ({r.name})"
        if label not in out:
            out.append(label)
        if len(out) == MAX_SUGGESTIONS:
            break
    return out


async def _lookup(query: str) -> tuple[CompanyRef, http.FetchResult]:
    """The company for `query` plus the ticker-table fetch result (for source metadata)."""
    rows, res = await get_ticker_table()
    found = _match(rows, query)
    if found is None:
        what = f"company matching {query.strip()!r}"
        suggestions = _suggest(rows, query)
        if suggestions:
            what += f" (did you mean {', '.join(suggestions)}?)"
        raise NotFound(what, SOURCE)
    return found, res


async def get_company(query: str) -> CompanyRef:
    """Find a company by ticker (BRK.B / BRK-B), CIK digits, or company name.

    Name matching applies only to name-like queries (see `_looks_like_name`); an unknown
    ticker raises NotFound listing up to 3 suggestions.
    """
    return (await _lookup(query))[0]


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


def _older_pages(filings_block: dict[str, Any]) -> list[dict[str, Any]]:
    """Older submission pages, newest first."""
    pages = list(filings_block.get("files", []))
    pages.sort(key=lambda p: p.get("filingTo") or "", reverse=True)
    return pages


async def _collect_filings(
    cik: int | str,
    forms: list[str] | None,
    since: date | None,
    limit: int | None,
    max_extra_pages: int,
) -> tuple[list[Filing], bool, Submissions, bool]:
    """`list_filings_data` plus a last flag: True when form-driven paging hit the page cap."""
    sub = await get_submissions(cik)
    cik10 = pad_cik(cik)
    filings_block = sub.data.get("filings", {})
    rows = _rows(filings_block.get("recent", {}), cik10)
    wanted = {f.strip().upper() for f in forms if f.strip()} if forms else None

    def keep(f: Filing) -> bool:
        return (wanted is None or f.form.upper() in wanted) and (
            since is None or f.filing_date >= since
        )

    capped = False
    if since is not None:
        earliest = min((f.filing_date for f in rows), default=None)
        if earliest is None or earliest > since:
            for page in _older_pages(filings_block):
                page_to = _parse_date(page.get("filingTo"))
                if page_to is not None and page_to < since:
                    continue
                rows.extend(_rows(await _get_submissions_page(page["name"]), cik10))
    elif wanted is not None:
        # Heavy filers (JPM files hundreds of 424B2s a month) may have only a few periodic
        # forms in `recent`. Page back, newest first, until `limit` matches are found (at
        # least one when limit is None), fetching at most `max_extra_pages` pages.
        target = limit if limit is not None else 1
        found = sum(1 for f in rows if keep(f))
        pages = _older_pages(filings_block)
        fetched = 0
        while found < target and fetched < len(pages):
            if fetched >= max_extra_pages:
                capped = True
                break
            page_rows = _rows(await _get_submissions_page(pages[fetched]["name"]), cik10)
            fetched += 1
            rows.extend(page_rows)
            found += sum(1 for f in page_rows if keep(f))

    out = [f for f in rows if keep(f)]
    out.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
    truncated = capped
    if limit is not None and len(out) > limit:
        out, truncated = out[:limit], True
    return out, truncated, sub, capped


async def list_filings_data(
    cik: int | str,
    forms: list[str] | None = None,
    since: date | None = None,
    limit: int | None = None,
    *,
    max_extra_pages: int = MAX_EXTRA_PAGES,
) -> tuple[list[Filing], bool, Submissions]:
    """Filings newest first, filtered by form and filing date >= since.

    Returns (filings, truncated, submissions). Older submission pages are fetched when
    `since` reaches earlier than the `recent` block, or when a `forms` filter finds fewer
    than `limit` matches in `recent` (no match at all, when limit is None). Form-driven
    paging stops after `max_extra_pages` pages; `truncated` is then True because older
    matches may exist.
    """
    out, truncated, sub, _ = await _collect_filings(cik, forms, since, limit, max_extra_pages)
    return out, truncated, sub


# ---------------------------------------------------------------- tools


async def resolve_ticker(query: str) -> ResolvedTicker:
    """Resolve a ticker (e.g. MSFT, BRK.B) or company name to its SEC CIK, exchange and SIC
    industry code. Source: SEC EDGAR."""
    if not query or not query.strip():
        raise BadInput("query must not be empty")
    company, tickers_res = await _lookup(query)  # the same lookup get_company uses
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
    items, truncated, sub, capped = await _collect_filings(
        company.cik, forms, since_date, limit, MAX_EXTRA_PAGES
    )
    latest = max((f.filing_date for f in items), default=None)
    notes = []
    if capped:
        notes.append(
            f"Searched the recent block and {MAX_EXTRA_PAGES} older submission pages only; "
            "older matching filings may exist. Pass `since` to search further back."
        )
    return FilingList(
        source=SOURCE,
        source_url=sub.url,
        retrieved_at=sub.fetched_at,
        as_of=latest,
        stale=sub.stale,
        notes=notes,
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
