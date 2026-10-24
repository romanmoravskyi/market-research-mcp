"""Record real FRED responses into tests/fixtures/fred/ (replaces the synthetic fixtures).

Usage (needs FRED_API_KEY in the environment or .env):

    uv run python scripts/record_fred_fixtures.py [SERIES_ID ...]

Only response bodies are stored. The API key is never written: it is removed from any
URL-like value and from the raw text before saving. Nothing is printed except file names.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from market_research_mcp.config import require
from market_research_mcp.logging_setup import redact

API_BASE = "https://api.stlouisfed.org/fred"
OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "fred"
DEFAULT_IDS = ("DGS10", "CPIAUCSL")
OBS_LIMIT = 6
UNKNOWN_ID = "NOSUCHSERIES123"


def _scrub(text: str, key: str) -> str:
    return redact(text).replace(key, "***")


def _get(client: httpx.Client, path: str, params: dict[str, str | int], key: str) -> str:
    resp = client.get(f"{API_BASE}/{path}", params={**params, "api_key": key, "file_type": "json"})
    if resp.status_code not in (200, 400):
        raise SystemExit(f"FRED returned HTTP {resp.status_code} for {path} {params}")
    return resp.text


def _write(name: str, body: str, key: str) -> None:
    data = json.loads(_scrub(body, key))
    path = OUT_DIR / name
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}", file=sys.stderr)


def main(argv: list[str]) -> None:
    load_dotenv(override=False)
    key = require("FRED_API_KEY")
    ids = [a.upper() for a in argv] or list(DEFAULT_IDS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=20.0) as client:
        for sid in ids:
            _write(f"{sid}_series.json", _get(client, "series", {"series_id": sid}, key), key)
            obs = _get(
                client,
                "series/observations",
                {"series_id": sid, "sort_order": "desc", "limit": OBS_LIMIT},
                key,
            )
            _write(f"{sid}_observations.json", obs, key)
        err = _get(client, "series", {"series_id": UNKNOWN_ID}, key)
        _write("error_unknown_series.json", err, key)
    print("Update tests/fixtures/fred/README.md: fixtures are now recorded.", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
