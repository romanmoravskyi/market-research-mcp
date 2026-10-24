"""MCP server entry point. Each module in SOURCE_MODULES exposes `register(mcp) -> None`.
To add a data source, create `sources/<name>.py` with `register` and add one line below.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from market_research_mcp import prompts
from market_research_mcp.config import load_settings
from market_research_mcp.logging_setup import setup_logging
from market_research_mcp.sources import (
    calendar,
    edgar,
    edgar_docs,
    fred,
    health,
    insiders,
    prices,
    xbrl,
)

SERVER_NAME = "market-research"

SOURCE_MODULES = [
    health,
    edgar,
    fred,
    prices,
    edgar_docs,
    insiders,
    xbrl,
    calendar,
    prompts,
]

logger = logging.getLogger(__name__)


def build_server() -> FastMCP:
    mcp = FastMCP(SERVER_NAME)
    for module in SOURCE_MODULES:
        module.register(mcp)
    return mcp


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    mcp = build_server()
    logger.info("Starting %s over stdio", SERVER_NAME)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
