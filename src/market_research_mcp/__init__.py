"""Read-only MCP server exposing market data to Claude."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("market-research-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    __version__ = "0.0.0"

__all__ = ["__version__"]
