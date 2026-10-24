"""Configuration loaded from environment variables and optional `.env` files.

`.env` files are read in this order; a value already set wins, so real environment variables
beat every file and earlier files beat later ones:

1. `MRM_ENV_FILE` if set, otherwise the per-user file `user_config_dir() / ".env"`
   (`%APPDATA%/market-research-mcp/.env` on Windows, `~/.config/market-research-mcp/.env`
   elsewhere, honouring `XDG_CONFIG_HOME`).
2. `.env` in the current working directory.

A `.env` inside `CLAUDE_PLUGIN_ROOT` is never read: the plugin directory is replaced on every
plugin update. Missing keys never crash startup. Tools call `require(name)` when they need one.

`python -m market_research_mcp.config journal-dir` prints JOURNAL_DIR (or nothing) for the
analyze-ticker skill, using the same sources.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

APP_DIR_NAME = "market-research-mcp"

ENV_VARS = (
    "SEC_USER_AGENT",
    "FRED_API_KEY",
    "PRICE_PROVIDER",
    "TIINGO_API_KEY",
    "FINNHUB_API_KEY",
    "CACHE_PATH",
    "LOG_LEVEL",
    "JOURNAL_DIR",
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
    journal_dir: Path | None = None


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def user_config_dir(windows: bool | None = None) -> Path:
    """Per-user config folder: %APPDATA%/market-research-mcp or ~/.config/market-research-mcp."""
    if windows is None:
        windows = os.name == "nt"
    if windows:
        base = _env("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = _env("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / APP_DIR_NAME


def dotenv_paths() -> list[Path]:
    """Candidate `.env` files in priority order, excluding anything in CLAUDE_PLUGIN_ROOT."""
    explicit = _env("MRM_ENV_FILE")
    first = Path(explicit).expanduser() if explicit else user_config_dir() / ".env"
    candidates = [first, Path.cwd() / ".env"]
    plugin_root = _env("CLAUDE_PLUGIN_ROOT")
    blocked = Path(plugin_root).resolve() if plugin_root else None
    out: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if blocked is not None and resolved.parent == blocked:
            continue
        if resolved not in out:
            out.append(resolved)
    return out


def load_env_files() -> None:
    """Load each existing `.env` from `dotenv_paths()` without overriding values already set."""
    for path in dotenv_paths():
        if path.is_file():
            load_dotenv(path, override=False)


def load_settings(dotenv: bool = True) -> Settings:
    """Build Settings from the environment, after loading `.env` files (see module doc)."""
    if dotenv:
        load_env_files()
    values: dict[str, object] = {}
    for name in ENV_VARS:
        value = _env(name)
        if value is not None:
            values[name.lower()] = value
    for key in ("cache_path", "journal_dir"):
        if key in values:
            values[key] = Path(str(values[key])).expanduser()
    return Settings(**values)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide Settings, loaded once. Call `get_settings.cache_clear()` in tests."""
    return load_settings()


def require(name: str) -> str:
    """Return the value of env var `name` or raise ConfigError naming it."""
    value = _env(name)
    if value is None:
        raise ConfigError(
            f"Missing required environment variable {name}. "
            "Set it in your environment or in .env (see .env.example)."
        )
    return value


def main(argv: list[str] | None = None) -> int:
    """CLI: `journal-dir` prints the configured JOURNAL_DIR, or an empty line if unset."""
    args = sys.argv[1:] if argv is None else argv
    if args != ["journal-dir"]:
        print("usage: python -m market_research_mcp.config journal-dir", file=sys.stderr)
        return 2
    journal = load_settings().journal_dir
    print(journal if journal else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
