"""Configuration loaded from environment variables (and `.env` if present).

Missing keys never crash startup. Tools call `require(name)` when they need a value.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

ENV_VARS = (
    "SEC_USER_AGENT",
    "FRED_API_KEY",
    "PRICE_PROVIDER",
    "TIINGO_API_KEY",
    "FINNHUB_API_KEY",
    "CACHE_PATH",
    "LOG_LEVEL",
)


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or empty."""


class Settings(BaseModel):
    sec_user_agent: str | None = None
    fred_api_key: str | None = None
    price_provider: str = "tiingo"
    tiingo_api_key: str | None = None
    finnhub_api_key: str | None = None
    cache_path: Path = Path.home() / ".market-research" / "cache.db"
    log_level: str = "INFO"


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_settings(dotenv: bool = True) -> Settings:
    """Build Settings from the environment. Loads `.env` without overriding real env vars."""
    if dotenv:
        load_dotenv(override=False)
    values: dict[str, object] = {}
    for name in ENV_VARS:
        value = _env(name)
        if value is not None:
            values[name.lower()] = value
    if "cache_path" in values:
        values["cache_path"] = Path(str(values["cache_path"])).expanduser()
    return Settings(**values)


def require(name: str) -> str:
    """Return the value of env var `name` or raise ConfigError naming it."""
    value = _env(name)
    if value is None:
        raise ConfigError(
            f"Missing required environment variable {name}. "
            "Set it in your environment or in .env (see .env.example)."
        )
    return value
