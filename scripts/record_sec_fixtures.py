"""Record real SEC EDGAR responses into tests/fixtures/sec/.

Usage (from the repo root):
    $env:SEC_USER_AGENT = "Your Name your.email@example.com"   # PowerShell
    uv run python scripts/record_sec_fixtures.py

Downloads company_tickers_exchange.json, submissions JSON for MSFT, JPM, O and BRK-B, and
company facts for MSFT, AAPL, JPM, BRK-B, O and NVDA, and the EDGAR company browse Atom
feed of JPM 10-K filings (`--atom-only` records just that feed). Every file is written
gzip-compressed (`*.json.gz`, `*.xml.gz`) to keep the repo small. Requests are limited to
10 per second as SEC asks. The User-Agent is read from the environment only and is never
written to disk or printed.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from market_research_mcp.sources import edgar

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "sec"

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS = {"MSFT": 789019, "JPM": 19617, "O": 726728, "BRK-B": 1067983}
COMPANYFACTS = {
    "MSFT": 789019,
    "AAPL": 320193,
    "JPM": 19617,
    "BRK-B": 1067983,
    "O": 726728,
    "NVDA": 1045810,
}
# EDGAR company browse Atom feeds: (CIK, form type)
ATOM_FEEDS = [(19617, "10-K")]
ATOM_MAX_PAGES = 3  # feed pages (of edgar.ATOM_COUNT entries) to keep per feed
MIN_INTERVAL = 0.1  # 10 requests per second
MAX_OLDER_PAGES = 1  # older submissions pages to keep per company


class Client:
    def __init__(self, user_agent: str) -> None:
        self._client = httpx.Client(
            timeout=60,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True,
        )
        self._last = 0.0

    def get(self, url: str) -> bytes:
        wait = self._last + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self._client.get(url)
        resp.raise_for_status()
        print(f"GET {url} -> {resp.status_code} ({len(resp.content)} bytes)")
        return resp.content

    def close(self) -> None:
        self._client.close()


def write_json_gz(path: Path, raw: bytes) -> None:
    """Validate JSON and write it gzip-compressed (deterministic: mtime=0) to `<path>.gz`."""
    data = json.loads(raw)
    text = json.dumps(data, indent=1) + "\n"
    path.with_name(path.name + ".gz").write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))


def atom_fixture_name(cik: int, form: str, start: int = 0) -> str:
    suffix = f"-start{start}" if start else ""
    return f"atom-CIK{cik:010d}-{form.replace('/', '_')}{suffix}.xml.gz"


def record_atom_feeds(client: Client) -> None:
    """Record EDGAR browse Atom feed pages raw (validated as XML), gzip-compressed, until a
    short page (the end of the feed) or ATOM_MAX_PAGES."""
    for cik, form in ATOM_FEEDS:
        for page in range(ATOM_MAX_PAGES):
            start = page * edgar.ATOM_COUNT
            raw = client.get(edgar.atom_feed_url(cik, form, start))
            root = ET.fromstring(raw)  # validate before writing
            (OUT / atom_fixture_name(cik, form, start)).write_bytes(gzip.compress(raw, mtime=0))
            if len(root.findall("{http://www.w3.org/2005/Atom}entry")) < edgar.ATOM_COUNT:
                break


def main() -> int:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        print(
            "SEC_USER_AGENT is not set. SEC requires a User-Agent with your name and email, "
            'e.g. "Jane Doe jane@example.com". Set it in the environment and run again.',
            file=sys.stderr,
        )
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "companyfacts").mkdir(exist_ok=True)
    client = Client(user_agent)
    try:
        record_atom_feeds(client)
        if "--atom-only" in sys.argv[1:]:
            print(f"Done. Atom feeds written to {OUT}")
            return 0
        write_json_gz(OUT / "company_tickers_exchange.json", client.get(TICKERS_URL))

        for cik in SUBMISSIONS.values():
            name = f"CIK{cik:010d}.json"
            raw = client.get(f"https://data.sec.gov/submissions/{name}")
            write_json_gz(OUT / name, raw)
            for page in json.loads(raw).get("filings", {}).get("files", [])[:MAX_OLDER_PAGES]:
                page_raw = client.get(f"https://data.sec.gov/submissions/{page['name']}")
                write_json_gz(OUT / page["name"], page_raw)

        for cik in COMPANYFACTS.values():
            name = f"CIK{cik:010d}.json"
            raw = client.get(f"https://data.sec.gov/api/xbrl/companyfacts/{name}")
            json.loads(raw)  # validate before writing
            (OUT / "companyfacts" / f"{name}.gz").write_bytes(gzip.compress(raw, mtime=0))
    except httpx.HTTPError as exc:
        print(f"Download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    print(f"Done. Fixtures written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
