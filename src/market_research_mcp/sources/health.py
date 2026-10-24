"""Health check tool."""

from __future__ import annotations

from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP

from market_research_mcp import __version__


def ping() -> dict[str, str]:
    """Check that the market-research server is running."""
    return {
        "status": "ok",
        "server": "market-research",
        "version": __version__,
        "retrieved_at": datetime.now(UTC).isoformat(),
    }


def register(mcp: FastMCP) -> None:
    mcp.tool()(ping)
