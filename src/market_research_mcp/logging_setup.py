"""Logging to stderr only (stdout carries the MCP stdio transport) plus secret redaction."""

from __future__ import annotations

import logging
import re
import sys

SECRET_KEYS = ("token", "key", "api_key", "apikey", "t")

_SECRET_QS = re.compile(
    r"(?P<prefix>[?&](?:" + "|".join(SECRET_KEYS) + r")=)(?P<value>[^&#\s]*)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Mask query-string values for secret-looking keys (token, key, api_key, apikey, t)."""
    return _SECRET_QS.sub(lambda m: m.group("prefix") + "***", text)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO") -> None:
    """Send all logging to stderr, never stdout."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
