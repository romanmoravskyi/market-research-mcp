"""Errors that data-source tools raise. FastMCP turns raised exceptions into tool errors.

All messages are human-readable and never contain secrets or URLs carrying keys.
"""

from __future__ import annotations

from mcp.server.fastmcp.exceptions import ToolError as _FastMCPToolError

from market_research_mcp.config import ConfigError
from market_research_mcp.logging_setup import redact

__all__ = [
    "BadInput",
    "ConfigError",
    "NotFound",
    "ProviderDown",
    "RateLimited",
    "ToolError",
    "as_tool_error",
]


class ToolError(_FastMCPToolError):
    """Base class for errors a tool shows to the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


class NotFound(ToolError):
    """The provider has no data for the requested item (HTTP 404 or empty result)."""

    def __init__(self, what: str, source: str | None = None) -> None:
        where = f" at {source}" if source else ""
        super().__init__(f"Not found{where}: {what}.")
        self.what = what
        self.source = source


class RateLimited(ToolError):
    """The provider rate limit was hit and no cached copy is available."""

    def __init__(self, source: str, retry_after: float | None = None) -> None:
        hint = f" Try again in about {int(retry_after)} seconds." if retry_after else ""
        super().__init__(f"{source} rate limit reached and no cached copy is available.{hint}")
        self.source = source
        self.retry_after = retry_after


class ProviderDown(ToolError):
    """The provider failed (5xx, timeout, connection error, unexpected response)."""

    def __init__(
        self,
        source: str,
        detail: str = "",
        alternative: str | None = None,
        *,
        body: str | None = None,
    ) -> None:
        # `body`: the provider's error response text for HTTP 4xx (redacted, at most 500
        # chars, request URL removed). Kept out of the message; callers may inspect it.
        msg = f"{source} is unavailable"
        if detail:
            msg += f" ({detail})"
        msg += "."
        if alternative:
            msg += f" You can try {alternative} instead."
        super().__init__(msg)
        self.source = source
        self.detail = detail
        self.alternative = alternative
        self.body = body


class BadInput(ToolError):
    """The tool arguments are invalid (unknown ticker format, bad date range, ...)."""

    def __init__(self, message: str) -> None:
        super().__init__(f"Invalid input: {message}")


def as_tool_error(exc: BaseException) -> str:
    """Return the message FastMCP should surface for `exc`, with secrets redacted."""
    if isinstance(exc, ToolError | ConfigError):
        return redact(str(exc))
    return f"Unexpected error ({type(exc).__name__}). Details are in the server log."
