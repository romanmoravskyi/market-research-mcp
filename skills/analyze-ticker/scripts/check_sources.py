"""Check that every financial number in a research one-pager is sourced.

Usage: uv run python check_sources.py <path/to/TICKER-YYYY-MM-DD.md>

A line with a financial number passes if it contains a URL or a footnote ref ``[n]`` that
resolves to a numbered entry with a URL in the Sources section. Lines inside the Sources and
"Could not verify" sections are not checked for numbers. The file must also have a Sources
section and the footer line, and must not contain recommendation language.

Exit codes: 0 clean, 1 problems found (printed as ``line N: reason``), 2 usage error.

This is a heuristic linter, not a parser. Standard library only, so it runs under any
Python 3.12+ without installing this package.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

FOOTER = "Research tooling output, not financial advice."

URL_RE = re.compile(r"https?://[^\s)>\]]+")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
SOURCES_HEADING_RE = re.compile(r"^sources\b", re.IGNORECASE)
UNVERIFIED_HEADING_RE = re.compile(r"^could not verify\b", re.IGNORECASE)
SOURCE_ENTRY_RE = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[.)])\s+(.*)$")
FOOTNOTE_RE = re.compile(r"\[(\d+)\](?!\()")

# Tokens that contain digits but are not financial numbers. Order matters.
NON_FINANCIAL_PATTERNS = [
    URL_RE,
    re.compile(r"\]\([^)]*\)"),  # markdown link targets
    re.compile(r"\b\d{10}-\d{2}-\d{6}\b"),  # SEC accession number
    re.compile(r"\b\d{18}\b"),  # accession number without dashes
    re.compile(r"\bCIK[\s|:#]*\d{1,10}\b", re.IGNORECASE),
    re.compile(r"\b0\d{9}\b"),  # zero-padded 10-digit CIK
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:.]+Z?)?\b"),  # ISO dates / timestamps
    re.compile(r"\b\d{4}-\d{2}\b"),  # year-month
    FOOTNOTE_RE,
    re.compile(r"^\s*(?:[-*+]\s+)?\d+[.)]\s+"),  # list numbering
    re.compile(r"\b(?:10-K|10-Q|8-K|20-F|40-F|6-K|S-1|S-3|13F|13D|13G|DEF\s?14A)(?:/A)?\b", re.I),
    re.compile(r"\bForm\s+[345]\b", re.IGNORECASE),
    re.compile(r"\bItem\s+\d+[A-Z]?(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bEX-\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\b(?:FY|CY|Q[1-4]\s*FY|Q[1-4]|H[12])\s?'?\d{2,4}\b", re.IGNORECASE),
    re.compile(r"\b[QH][1-4]\b"),
    re.compile(r"\b\d{1,2}[ywd]\b"),  # period labels: 1y, 5y, 52w, 30d
    re.compile(r"\b(?:1|3|6|9|12)m\b"),  # period labels: 1m, 3m, 12m (lowercase only)
    re.compile(r"\b\d{1,3}\s+(?:months?|years?|quarters?|weeks?|days?)\b", re.I),  # durations
    re.compile(r"(?<![$\d.,])\b(?:19|20)\d{2}\b(?![\d.,%]|\s?[BMK]\b)"),  # bare years
]

# Phrases explicitly allowed even though they contain a banned word.
ALLOWED_PHRASES = [
    re.compile(r"open[- ]market\s+(?:buys?|purchases?)(?:\s*/\s*sells?)?", re.IGNORECASE),
    re.compile(r"open[- ]market\s+(?:or\s+private\s+)?(?:purchase|sale)", re.IGNORECASE),
    re.compile(r"\bbuy[- ]?backs?\b", re.IGNORECASE),
]

BANNED_PHRASES = [
    "strong buy",
    "sell rating",
    "price target",
    "undervalued",
    "overvalued",
    "we recommend",
    "should buy",
    "should sell",
    "buy",
]
BANNED_RES = [(p, re.compile(rf"\b{re.escape(p)}\b", re.IGNORECASE)) for p in BANNED_PHRASES]


@dataclass(frozen=True)
class Problem:
    line: int
    reason: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.reason}"


def _section_of(lines: list[str]) -> list[str]:
    """Return, per line, which section it belongs to: 'sources', 'unverified' or 'body'."""
    out: list[str] = []
    current = "body"
    in_fence = False
    for raw in lines:
        if raw.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(raw)
        if m:
            title = m.group(1).strip()
            if SOURCES_HEADING_RE.match(title):
                current = "sources"
            elif UNVERIFIED_HEADING_RE.match(title):
                current = "unverified"
            else:
                current = "body"
            out.append("heading")
            continue
        out.append(current)
    return out


def parse_sources(lines: list[str], sections: list[str]) -> dict[int, bool]:
    """Map source number -> whether that entry contains a URL."""
    entries: dict[int, bool] = {}
    for raw, sec in zip(lines, sections, strict=True):
        if sec != "sources":
            continue
        m = SOURCE_ENTRY_RE.match(raw)
        if m:
            n = int(m.group(1) or m.group(2))
            entries[n] = bool(URL_RE.search(m.group(3)))
    return entries


def has_financial_number(line: str) -> bool:
    stripped = line
    if re.fullmatch(r"\s*\|?[\s:|-]*\|?\s*", stripped):  # table separator / blank
        return False
    if re.fullmatch(r"\s*[A-Z]{1,5}(?:\.[A-Z])?\s*", stripped):  # ticker-only line
        return False
    for pat in NON_FINANCIAL_PATTERNS:
        stripped = pat.sub(" ", stripped)
    return bool(re.search(r"\d", stripped))


def banned_in(line: str) -> list[str]:
    text = URL_RE.sub(" ", line)
    for pat in ALLOWED_PHRASES:
        text = pat.sub(" ", text)
    hits: list[str] = []
    for phrase, pat in BANNED_RES:
        if pat.search(text):
            hits.append(phrase)
            text = pat.sub(" ", text)  # avoid double-reporting "buy" inside "strong buy"
    return hits


def check_text(text: str) -> list[Problem]:
    lines = text.splitlines()
    sections = _section_of(lines)
    sources = parse_sources(lines, sections)
    problems: list[Problem] = []

    if "sources" not in sections:
        problems.append(Problem(0, "missing Sources section (heading '## Sources')"))
    non_empty = [ln.strip() for ln in lines if ln.strip()]
    if not non_empty or FOOTER not in " ".join(non_empty[-3:]):
        problems.append(Problem(0, f"missing footer line: '{FOOTER}'"))

    for idx, (raw, sec) in enumerate(zip(lines, sections, strict=True), start=1):
        for phrase in banned_in(raw):
            problems.append(Problem(idx, f"banned phrase '{phrase}'"))

        if sec in ("sources", "unverified"):
            continue
        check_line = raw
        if sec == "heading":
            # Years in headings are fine; other numbers in headings still need a source.
            check_line = re.sub(r"\b(?:19|20)\d{2}\b", " ", raw)
        if not has_financial_number(check_line):
            continue
        if URL_RE.search(raw):
            continue
        refs = [int(n) for n in FOOTNOTE_RE.findall(raw)]
        if not refs:
            problems.append(Problem(idx, "number without source URL or footnote ref [n]"))
            continue
        good = [n for n in refs if sources.get(n)]
        if not good:
            bad = ", ".join(f"[{n}]" for n in refs)
            problems.append(
                Problem(idx, f"footnote {bad} does not resolve to a Sources entry with a URL")
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_sources.py <file.md>", file=sys.stderr)
        return 2
    path = Path(args[0])
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    problems = check_text(path.read_text(encoding="utf-8"))
    if not problems:
        print(f"OK: {path} - all numbers sourced, Sources and footer present.")
        return 0
    for p in problems:
        print(p)
    print(f"{len(problems)} problem(s) in {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
