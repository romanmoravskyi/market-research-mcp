"""MCP prompts: the analyze-ticker research procedure for clients without skill support.

The procedure text is `analyze_ticker.md` (package data), a copy of the body of
`skills/analyze-ticker/SKILL.md`. `tests/test_prompts.py` fails if the two drift apart.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files

from mcp.server.fastmcp import FastMCP


@cache
def analyze_ticker_procedure() -> str:
    """Return the analyze-ticker procedure (SKILL.md body without frontmatter)."""
    return files("market_research_mcp").joinpath("analyze_ticker.md").read_text(encoding="utf-8")


def analyze_ticker_text(ticker: str) -> str:
    """Return the prompt text: the ticker to research, then the full procedure."""
    symbol = ticker.strip().upper()
    header = (
        f"Produce a sourced one-page research brief for ticker {symbol} by following the "
        "procedure below exactly. Facts only, not financial advice.\n\n"
    )
    return header + analyze_ticker_procedure()


def analyze_ticker(ticker: str) -> str:
    """Sourced one-page research brief for a US-listed ticker. Facts only, no recommendations."""
    return analyze_ticker_text(ticker)


def register(mcp: FastMCP) -> None:
    mcp.prompt(name="analyze_ticker")(analyze_ticker)
