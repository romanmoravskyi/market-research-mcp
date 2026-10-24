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
    re.compile(r"\bSIC[\s|:#]*\d{4}\b"),  # SIC industry code
    re.compile(r"\b0\d{9}\b"),  # zero-padded 10-digit CIK
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:.]+Z?)?\b"),  # ISO dates / timestamps
    re.compile(r"\b\d{4}-\d{2}\b"),  # year-month
    FOOTNOTE_RE,
    re.compile(r"^\s*(?:[-*+]\s+)?\d+[.)]\s+"),  # list numbering
    re.compile(r"\b(?:10-K|10-Q|8-K|20-F|40-F|6-K|S-1|S-3|13F|13D|13G|DEF\s?14A)(?:/A)?\b", re.I),
    re.compile(r"\bForm\s+[345]\b", re.IGNORECASE),
    re.compile(r"\bItem\s+\d+[A-Z]?(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bEX-\d+(?:\.\d+)?\b", re.IGNORECASE),
    # Fiscal periods: FY2025, Q3 FY2025, Q4 2025, Q3'25. A bare 2-digit number after a
    # quarter label ("Q2 42%") is a metric, not a year, so it needs FY, an apostrophe or 4 digits.
    re.compile(r"\b(?:FY|CY)\s?'?(?:\d{4}|\d{2})\b(?![\d%]|[.,]\d)", re.IGNORECASE),
    re.compile(r"\b[QH][1-4]\s*(?:FY\s?'?\d{2,4}|'\d{2}|\d{4})\b(?![\d%]|[.,]\d)", re.I),
    re.compile(r"\b[QH][1-4]\b"),
    re.compile(r"(?<![$\d.,])\b\d{1,2}[ywd]\b"),  # period labels: 1y, 5y, 52w, 30d
    re.compile(r"(?<![$\d.,])\b(?:1|3|6|9|12)m\b"),  # period labels: 1m, 3m, 12m (not $3m)
    # Durations only as look-back windows ("last 12 months", "within 120 days", "(12 months)").
    re.compile(
        r"(?:\b(?:within|last|past|next|over|trailing)\s+|\()"
        r"\d{1,3}\s+(?:months?|years?|quarters?|weeks?|days?)\b\)?",
        re.IGNORECASE,
    ),
    # Years only in date context ("in 2025", "since 2021", "fiscal 2024", "December 2025").
    re.compile(
        r"\b(?:in|since|during|until|through|fiscal|calendar|year|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?,?\s+"
        r"(?:\d{1,2},?\s+)?(?:19|20)\d{2}\b(?![\d%]|[.,]\d|\s?[BMK]\b)",
        re.IGNORECASE,
    ),
    re.compile(r"\((?:19|20)\d{2}\)"),  # "(2025)"
    # Month and day without a year, e.g. a fiscal year end "June 30". Exempt only when the
    # day ends the phrase (end of line, ",", ";", "(", ")" or a sentence-ending "."), so
    # "June 30 bn" or "May 15 million" still count as numbers. Capitalized month names
    # only, so the verb "may" before a number is not taken for a date.
    re.compile(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
        r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2}"
        r"(?=\s*(?:$|[,;()]|\.(?:\s|$)))"
    ),
    # Year-to-date period labels ("9M YTD") only as a label: at the line start, in
    # parentheses or after a function word. "Net loss 3M YTD" or "EUR 9M YTD" still count.
    re.compile(
        r"(?:^|\(|\b(?:the|in|for|over|from|minus|and)\s+)(?:3|6|9)M\s+YTD\b", re.IGNORECASE
    ),
    re.compile(r"\b10b5-1\b", re.IGNORECASE),  # SEC Rule 10b5-1
]

# Phrases explicitly allowed even though they contain a banned word.
ALLOWED_PHRASES = [
    re.compile(r"open[- ]market\s+(?:buys?|purchases?)(?:\s*/\s*sells?)?", re.IGNORECASE),
    re.compile(r"open[- ]market\s+(?:or\s+private\s+)?(?:purchase|sale)", re.IGNORECASE),
    re.compile(r"\bbuy[- ]?backs?\b", re.IGNORECASE),
]

# Recommendation language only, as phrases, so names like "Best Buy" and words like
# "buy-side" or "insiders sell" pass. Longer phrases first so each hit is reported once.
BANNED_PHRASES = [
    "we recommend buying",
    "we recommend selling",
    "we recommend",
    "strong buy",
    "strong sell",
    "buy rating",
    "sell rating",
    "hold rating",
    "should buy",
    "should sell",
    "price target",
    "undervalued",
    "overvalued",
    "overweight",
    "underweight",
    "outperform",
    "underperform",
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
            # Leave ISO dates intact for NON_FINANCIAL_PATTERNS ("2026-09-01" is a date).
            # Year ranges ("2021-2025") go first, then single years outside ISO dates.
            check_line = re.sub(r"\b(?:19|20)\d{2}\s*[-–]\s*(?:19|20)\d{2}\b(?!-\d)", " ", raw)
            check_line = re.sub(r"(?<![\d-])\b(?:19|20)\d{2}\b(?!-\d)", " ", check_line)
        if not has_financial_number(check_line):
            continue
        if URL_RE.search(raw):
            continue
        refs = [int(n) for n in FOOTNOTE_RE.findall(raw)]
        if not refs:
            problems.append(Problem(idx, "number without source URL or footnote ref [n]"))
            continue
        bad_refs = [n for n in dict.fromkeys(refs) if not sources.get(n)]
        if bad_refs:
            bad = ", ".join(f"[{n}]" for n in bad_refs)
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
