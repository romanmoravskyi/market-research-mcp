"""SEC EDGAR filing text: 10-K/10-Q sections and exhibits as plain text, in chunks.

Documents are fetched through `http.fetch("edgar", ...)` (User-Agent, rate limit, retries).
Section extraction uses edgartools' TenK/TenQ parsers on the HTML we fetched, so edgartools
makes no network calls of its own. Filed documents never change, so extracted text is cached
forever in the `filing_docs` namespace.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

from market_research_mcp import cache as cache_mod
from market_research_mcp import http
from market_research_mcp.config import require
from market_research_mcp.errors import BadInput, NotFound
from market_research_mcp.models import Sourced, utcnow
from market_research_mcp.sources.edgar import ARCHIVES_BASE, SOURCE, get_company

__all__ = [
    "CHUNK_CHARS",
    "FilingText",
    "get_filing_exhibit",
    "get_filing_section",
    "register",
    "split_chunks",
]

CHUNK_CHARS = 24_000  # about 6-8k tokens
NS = "filing_docs"

# ---------------------------------------------------------------- section vocabulary

_TENK_ITEMS = [
    "1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A",
    "8", "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16",
]  # fmt: skip
_TENQ_ITEMS = ["I-1", "I-2", "I-3", "I-4", "II-1", "II-1A", "II-2", "II-3", "II-4", "II-5", "II-6"]

# Friendly name -> (10-K item, 10-Q item). None = not part of that form.
_ALIASES: dict[str, tuple[str | None, str | None]] = {
    "business": ("1", None),
    "risk factors": ("1A", "II-1A"),
    "unresolved staff comments": ("1B", None),
    "cybersecurity": ("1C", None),
    "properties": ("2", None),
    "legal proceedings": ("3", "II-1"),
    "mine safety disclosures": ("4", "II-4"),
    "market for common equity": ("5", None),
    "md&a": ("7", "I-2"),
    "mda": ("7", "I-2"),
    "management's discussion and analysis": ("7", "I-2"),
    "market risk": ("7A", "I-3"),
    "financial statements": ("8", "I-1"),
    "controls and procedures": ("9A", "I-4"),
    "other information": ("9B", "II-5"),
    "executive compensation": ("11", None),
    "exhibits": ("15", "II-6"),
}
_FRIENDLY_NAMES = ["Risk Factors", "MD&A", "Business", "Market Risk", "Financial Statements"]


def _norm(value: str) -> str:
    v = value.strip().lower().replace("’", "'").replace("_", " ")
    v = re.sub(r"\s+", " ", v)
    return v.replace("&amp;", "&")


def _supported_message() -> str:
    return (
        "unknown section. Use a 10-K item (1, 1A, 1B, 1C, 2, 3, 7, 7A, 8, 9A ... 16, "
        "also as 'Item 1A'), a 10-Q item ('Part I Item 2', 'Part II Item 1A'), or a name: "
        + ", ".join(_FRIENDLY_NAMES)
        + ". On a 10-Q, a bare number means Part I (2 = MD&A) except 1A (Part II)."
    )


def _resolve_section(section: str, form: str | None) -> str | None:
    """Canonical item key for `form` ('10-K' or '10-Q'); with form=None only checks validity.

    Returns None when the name is valid in general but not for this form.
    Raises BadInput for names that are valid for no form.
    """
    s = _norm(section)
    s = re.sub(r"^item\s*", "", s).strip(" .")
    tenk: str | None = None
    tenq: str | None = None
    m = re.fullmatch(r"part\s*(i{1,2}|1|2)\s*,?\s*(?:item\s*)?(\d{1,2}[a-c]?)", s)
    if m:
        part = "I" if m.group(1) in ("i", "1") else "II"
        tenq = f"{part}-{m.group(2).upper()}"
        if tenq not in _TENQ_ITEMS:
            raise BadInput(_supported_message())
    elif re.fullmatch(r"\d{1,2}[a-c]?", s):
        item = s.upper()
        tenk = item if item in _TENK_ITEMS else None
        if item == "1A":
            tenq = "II-1A"
        elif f"I-{item}" in _TENQ_ITEMS:
            tenq = f"I-{item}"
        elif f"II-{item}" in _TENQ_ITEMS:
            tenq = f"II-{item}"
        if tenk is None and tenq is None:
            raise BadInput(_supported_message())
    elif s in _ALIASES:
        tenk, tenq = _ALIASES[s]
    elif s.startswith("management") and "discussion" in s:
        tenk, tenq = _ALIASES["md&a"]
    else:
        raise BadInput(_supported_message())
    if form is None:
        return tenk or tenq
    return tenk if form == "10-K" else tenq


def _label(key: str) -> str:
    if "-" in key:
        part, item = key.split("-", 1)
        return f"Part {part}, Item {item}"
    return f"Item {key}"


def _base_form(form: str) -> str | None:
    f = form.upper().removesuffix("/A")
    return f if f in ("10-K", "10-Q") else None


# ---------------------------------------------------------------- models


class FilingText(Sourced):
    """One chunk of a filing section or exhibit as plain text."""

    ticker: str
    cik: str
    accession: str
    form: str
    section: str | None = None
    exhibit: str | None = None
    text: str
    chunk_index: int
    total_chunks: int
    total_chars: int
    next_cursor: str | None = None


@dataclass(frozen=True)
class IndexDoc:
    seq: str
    description: str
    filename: str
    type: str


@dataclass(frozen=True)
class FilingIndex:
    form: str
    filing_date: date | None
    documents: list[IndexDoc]
    url: str
    filer_ciks: frozenset[str] = frozenset()  # 10-digit CIKs listed as filers on the index


# ---------------------------------------------------------------- helpers


def normalize_accession(accession: str) -> str:
    """'0001193125-26-323660' or '000119312526323660' -> '0001193125-26-323660'."""
    digits = re.sub(r"[\s-]", "", accession or "")
    if not re.fullmatch(r"\d{18}", digits):
        raise BadInput(
            f"accession must be 18 digits, with or without dashes "
            f"(e.g. 0001193125-26-323660), got {accession!r}"
        )
    return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"


def _folder_url(cik: str, accession: str) -> str:
    return f"{ARCHIVES_BASE}{int(cik)}/{accession.replace('-', '')}/"


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split into chunks of at most `limit` chars, on paragraph breaks where possible.

    Lossless: "".join(result) == text.
    """
    if not text:
        return [""]
    pieces: list[str] = []
    for para in re.split(r"(?<=\n\n)", text):
        if len(para) <= limit:
            pieces.append(para)
            continue
        for line in re.split(r"(?<=\n)", para):  # oversized paragraph: lines, then hard cuts
            pieces.extend(line[i : i + limit] for i in range(0, len(line), limit))
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > limit:
            chunks.append(current)
            current = ""
        current += piece
    if current:
        chunks.append(current)
    return chunks


def _encode_cursor(accession: str, kind: str, index: int) -> str:
    raw = json.dumps({"a": accession, "k": kind, "i": index}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, accession: str, kind: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        index = int(data["i"])
        ok = data["a"] == accession and data["k"] == kind and index >= 0
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        ok = False
    if not ok:
        raise BadInput("cursor does not belong to this filing and section; omit it to restart")
    return index


def _set_edgartools_identity() -> None:
    # Set the env var directly: edgar.set_identity() logs the identity at INFO level.
    os.environ["EDGAR_IDENTITY"] = require("SEC_USER_AGENT")


def _parse_index(html: bytes, url: str) -> FilingIndex:
    soup = BeautifulSoup(html, "lxml")
    form_el = soup.select_one("#formName")
    form = ""
    if form_el is not None:
        m = re.search(r"Form\s+(\S+)", form_el.get_text(" ", strip=True))
        form = m.group(1) if m else ""
    filing_date: date | None = None
    for group in soup.select("div.formGrouping"):
        divs = [d.get_text(" ", strip=True) for d in group.find_all("div")]
        for head, value in zip(divs, divs[1:], strict=False):
            if head == "Filing Date":
                try:
                    filing_date = date.fromisoformat(value[:10])
                except ValueError:
                    pass
    docs: list[IndexDoc] = []
    for row in soup.select("table.tableFile tr"):
        cells = row.find_all("td")
        if len(cells) < 4 or cells[2].find("a") is None:
            continue
        name = cells[2].get_text(" ", strip=True).split()
        if not name:
            continue
        docs.append(
            IndexDoc(
                seq=cells[0].get_text(strip=True),
                description=cells[1].get_text(" ", strip=True),
                filename=name[0],
                type=cells[3].get_text(strip=True),
            )
        )
    if not form and docs:
        form = docs[0].type
    filers: set[str] = set()
    for a in soup.select('a[href*="browse-edgar"]'):
        m = re.search(r"CIK=(\d+)", str(a.get("href", "")), re.IGNORECASE)
        if m:
            filers.add(m.group(1).zfill(10))
    return FilingIndex(
        form=form, filing_date=filing_date, documents=docs, url=url, filer_ciks=frozenset(filers)
    )


async def _get_index(cik: str, accession: str, ticker: str) -> FilingIndex:
    """The filing index, keyed by CIK + accession; BadInput if the filing is not this company's."""
    url = f"{_folder_url(cik, accession)}{accession}-index.htm"
    res = await http.fetch(
        "edgar", url, cache_ns=NS, cache_key=f"{cik}|{accession}|index", parse="bytes"
    )
    index = _parse_index(res.data, url)
    if index.filer_ciks and cik not in index.filer_ciks:
        raise BadInput(
            f"filing {accession} does not belong to {ticker} (CIK {cik}); "
            "get accession numbers for this company from list_filings."
        )
    return index


# A section set missing the requested item may be a parser miss: keep it only briefly.
PARTIAL_TTL_SECONDS = 24 * 3600


async def _cache_get(key: str) -> tuple[str, datetime] | None:
    hit = await asyncio.to_thread(cache_mod.get_cache().get, NS, key)
    if hit is None or hit.expired:
        return None
    value = hit.value if isinstance(hit.value, str) else hit.value.decode("utf-8")
    return value, hit.fetched_at


async def _cache_set(key: str, value: str, ttl: int | None | str = "default") -> datetime:
    now = utcnow()
    seconds = cache_mod.ttl_for(NS) if ttl == "default" else ttl
    await asyncio.to_thread(cache_mod.get_cache().set, NS, key, value, seconds, now)
    return now


class _HtmlFiling:
    """The few attributes edgartools' TenK/TenQ read, backed by HTML we already have."""

    def __init__(self, html: str, form: str) -> None:
        self._html = html
        self.form = form
        self.accession_number = ""
        self.base_dir = ""

    def html(self) -> str:
        return self._html


def _extract_sections(html: str, form: str) -> dict[str, str]:
    """All items of a 10-K or 10-Q as text, keyed by canonical item key. Blocking."""
    from edgar.company_reports import TenK, TenQ

    out: dict[str, str] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if form == "10-K":
            report: Any = TenK(_HtmlFiling(html, "10-K"))
            for key in _TENK_ITEMS:
                text = report.get(key)
                if text and text.strip():
                    out[key] = text
        else:
            report = TenQ(_HtmlFiling(html, "10-Q"))
            for key in _TENQ_ITEMS:
                part, item = key.split("-", 1)
                text = report.get_item_with_part(f"Part {part}", f"Item {item}")
                if text and text.strip():
                    out[key] = text
    return out


def _html_to_text(html: str) -> str:
    """Plain text from an HTML document (tables kept as text rows). Blocking."""
    from edgar.documents import parse_html

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        text = parse_html(html).text()
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _chunk_result(
    *,
    text: str,
    cursor: str | None,
    accession: str,
    kind: str,
    company_ticker: str,
    cik: str,
    index: FilingIndex,
    source_url: str,
    retrieved_at: datetime,
    section: str | None = None,
    exhibit: str | None = None,
) -> FilingText:
    chunks = split_chunks(text)
    i = _decode_cursor(cursor, accession, kind) if cursor else 0
    if i >= len(chunks):
        raise BadInput(f"cursor points past the last chunk ({len(chunks)} chunks)")
    nxt = _encode_cursor(accession, kind, i + 1) if i + 1 < len(chunks) else None
    return FilingText(
        source=SOURCE,
        source_url=source_url,
        retrieved_at=retrieved_at,
        as_of=index.filing_date,
        notes=[f"Filing index: {index.url}"],
        ticker=company_ticker,
        cik=cik,
        accession=accession,
        form=index.form,
        section=section,
        exhibit=exhibit,
        text=chunks[i],
        chunk_index=i,
        total_chunks=len(chunks),
        total_chars=len(text),
        next_cursor=nxt,
    )


# ---------------------------------------------------------------- tools


async def get_filing_section(
    ticker: str, accession: str, section: str, cursor: str | None = None
) -> FilingText:
    """Plain text of one section of a 10-K or 10-Q filing, in chunks of about 24,000
    characters. `section`: an item such as "1A", "Item 7", "7A", "1", "2", "Part II Item 1A",
    or a name such as "Risk Factors" or "MD&A" (10-Q: MD&A is Part I Item 2). Pass the
    returned `next_cursor` to get the next chunk. Get accession numbers from list_filings.
    Source: SEC EDGAR."""
    if not section or not section.strip():
        raise BadInput("section must not be empty")
    _resolve_section(section, None)  # reject unknown names before any network call
    acc = normalize_accession(accession)
    company = await get_company(ticker)
    index = await _get_index(company.cik, acc, company.ticker)
    form = _base_form(index.form)
    if form is None:
        raise BadInput(
            f"section extraction supports 10-K and 10-Q filings; {acc} is a {index.form or '?'}. "
            "Use get_filing_exhibit for 8-K exhibits such as EX-99.1."
        )
    key = _resolve_section(section, form)
    if key is None:
        raise BadInput(f"{section!r} is not a {form} section. " + _supported_message())
    primary = next(
        (d for d in index.documents if d.type.upper() == index.form.upper()),
        index.documents[0] if index.documents else None,
    )
    if primary is None:
        raise NotFound(f"primary document in filing {acc}", SOURCE)
    doc_url = _folder_url(company.cik, acc) + primary.filename

    cache_key = f"{company.cik}|{acc}|sections"
    cached = await _cache_get(cache_key)
    if cached is not None:
        sections: dict[str, str] = json.loads(cached[0])
        retrieved_at = cached[1]
    else:
        _set_edgartools_identity()
        res = await http.fetch("edgar", doc_url, parse="bytes")
        sections = await asyncio.to_thread(_extract_sections, _decode(res.data), form)
        retrieved_at = utcnow()
        if sections:  # an empty extraction is a parser failure: never cache it
            partial = not sections.get(key, "").strip()
            ttl = PARTIAL_TTL_SECONDS if partial else "default"
            retrieved_at = await _cache_set(cache_key, json.dumps(sections), ttl)

    text = sections.get(key, "")
    if not text.strip():
        found = ", ".join(_label(k) for k in sections) or "none"
        raise NotFound(f"{_label(key)} in {form} {acc} (sections found: {found})", SOURCE)
    return _chunk_result(
        text=text,
        cursor=cursor,
        accession=acc,
        kind=f"s:{key}",
        company_ticker=company.ticker,
        cik=company.cik,
        index=index,
        source_url=doc_url,
        retrieved_at=retrieved_at,
        section=_label(key),
    )


def _normalize_exhibit(exhibit: str) -> str:
    e = re.sub(r"\s+", "", (exhibit or "").upper())
    if not e:
        raise BadInput("exhibit must not be empty (e.g. EX-99.1)")
    if re.match(r"^\d", e):
        e = "EX-" + e
    elif e.startswith("EX") and not e.startswith("EX-"):
        e = "EX-" + e[2:]
    return e


async def get_filing_exhibit(
    ticker: str, accession: str, exhibit: str, cursor: str | None = None
) -> FilingText:
    """Plain text of one exhibit of a filing (e.g. "EX-99.1", the press release attached to
    an earnings 8-K), in chunks of about 24,000 characters. Pass the returned `next_cursor`
    to get the next chunk. Get accession numbers from list_filings. Source: SEC EDGAR."""
    wanted = _normalize_exhibit(exhibit)
    acc = normalize_accession(accession)
    company = await get_company(ticker)
    index = await _get_index(company.cik, acc, company.ticker)
    doc = next((d for d in index.documents if d.type.upper() == wanted), None)
    if doc is None:
        present = sorted({d.type for d in index.documents if d.type.upper().startswith("EX-")})
        raise NotFound(
            f"{wanted} in filing {acc} (exhibits present: {', '.join(present) or 'none'})",
            SOURCE,
        )
    doc_url = _folder_url(company.cik, acc) + doc.filename
    ext = doc.filename.lower().rsplit(".", 1)[-1]
    if ext not in ("htm", "html", "txt", "xml"):
        raise BadInput(f"{wanted} is a .{ext} file and cannot be shown as text; open {doc_url}")

    cache_key = f"{company.cik}|{acc}|exhibit|{wanted}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        text, retrieved_at = cached
    else:
        res = await http.fetch("edgar", doc_url, parse="bytes")
        raw = _decode(res.data)
        text = raw if ext == "txt" else await asyncio.to_thread(_html_to_text, raw)
        retrieved_at = await _cache_set(cache_key, text)
    if not text.strip():
        raise NotFound(f"text in {wanted} of filing {acc}", SOURCE)
    return _chunk_result(
        text=text,
        cursor=cursor,
        accession=acc,
        kind=f"x:{wanted}",
        company_ticker=company.ticker,
        cik=company.cik,
        index=index,
        source_url=doc_url,
        retrieved_at=retrieved_at,
        exhibit=wanted,
    )


def register(mcp: FastMCP) -> None:
    mcp.tool()(get_filing_section)
    mcp.tool()(get_filing_exhibit)
