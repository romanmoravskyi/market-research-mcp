"""MCP server entry point. Each module in SOURCE_MODULES exposes `register(mcp) -> None`.
To add a data source, create `sources/<name>.py` with `register` and add one line below.
"""

from __future__ import annotations

import importlib
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
    ratios,
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
    ratios,
    calendar,
    prompts,
]

logger = logging.getLogger(__name__)


def build_server() -> FastMCP:
    mcp = FastMCP(SERVER_NAME)
    for module in SOURCE_MODULES:
        module.register(mcp)
    return mcp


# Modules the tools import lazily (inside functions). tests/test_server_startup.py fails if
# a new lazy import is not listed here. Optional ones are skipped when not installed.
PRELOAD_MODULES = ("edgar.company_reports", "edgar.documents", "yfinance")


def _preload_lazy_imports() -> None:
    """Import every lazily imported module before the stdio transport starts.

    On Windows the transport keeps a synchronous blocking read pending on stdin. Loading a
    native extension DLL for the first time (edgartools pulls in numpy, pandas, pyarrow and
    others) runs the DLL's C runtime start-up, which queries the standard handles and waits
    behind that read: the tool call hangs until the client times out. Doing the imports here,
    before any read is pending, avoids it. Costs about 4 s of startup, once.
    """
    for name in PRELOAD_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            logger.debug("optional module %s not installed; not preloaded", name)


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    _preload_lazy_imports()
    mcp = build_server()
    logger.info("Starting %s over stdio", SERVER_NAME)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
