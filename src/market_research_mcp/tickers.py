"""Shared ticker normalization for every data source."""

from __future__ import annotations

import re

from market_research_mcp.errors import BadInput

__all__ = ["canonical_ticker", "normalize_ticker"]

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,14}$")


def canonical_ticker(ticker: str) -> str:
    """Upper-case, strip, and use a dash for share classes ('brk.b', 'BRK/B' -> 'BRK-B').

    No validation; use `normalize_ticker` for user input.
    """
    return (ticker or "").strip().upper().replace(".", "-").replace("/", "-")


def normalize_ticker(ticker: str) -> str:
    """Canonical ticker ('brk.b' -> 'BRK-B'); raises BadInput if it is not a valid symbol."""
    t = canonical_ticker(ticker)
    if not _TICKER_RE.match(t):
        raise BadInput(f"ticker {ticker!r} is not a valid symbol (e.g. MSFT, BRK.B).")
    return t
