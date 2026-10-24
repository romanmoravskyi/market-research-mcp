"""Record real SEC EDGAR filing documents into tests/fixtures/sec/docs/ (gzip-compressed).

Usage (from the repo root; SEC_USER_AGENT comes from the environment or .env):
    uv run python scripts/record_doc_fixtures.py

Saves, for MSFT: the latest 10-K filing index page and primary document, and the latest
earnings 8-K (Item 2.02) filing index page and its EX-99.1 exhibit, plus `manifest.json`
naming them. Requests are limited to 10 per second. The User-Agent is never printed or saved.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "sec" / "docs"
CIK = 789019  # MSFT
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/"
MIN_INTERVAL = 0.1  # 10 requests per second


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


def folder(accession: str) -> str:
    return f"{ARCHIVES}{CIK}/{accession.replace('-', '')}/"


def index_url(accession: str) -> str:
    return f"{folder(accession)}{accession}-index.htm"


def doc_types(index_html: bytes) -> dict[str, str]:
    """Map document type (e.g. EX-99.1) to file name from a filing index page."""
    soup = BeautifulSoup(index_html, "lxml")
    out: dict[str, str] = {}
    for row in soup.select("table.tableFile tr"):
        cells = row.find_all("td")
        if len(cells) >= 4 and cells[2].find("a"):
            name = cells[2].get_text(strip=True).split()[0]
            out.setdefault(cells[3].get_text(strip=True), name)
    return out


def save(name: str, raw: bytes) -> None:
    (OUT / name).write_bytes(gzip.compress(raw, mtime=0))


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    if not user_agent:
        print("SEC_USER_AGENT is not set (environment or .env).", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    client = Client(user_agent)
    try:
        sub = json.loads(client.get(f"https://data.sec.gov/submissions/CIK{CIK:010d}.json"))
        recent = sub["filings"]["recent"]
        rows = [
            {k: recent[k][i] for k in ("form", "accessionNumber", "filingDate", "items")}
            | {"primaryDocument": recent["primaryDocument"][i]}
            for i in range(len(recent["accessionNumber"]))
        ]
        tenk = next(r for r in rows if r["form"] == "10-K")
        eightk = next(r for r in rows if r["form"] == "8-K" and "2.02" in (r["items"] or ""))

        tenk_index = client.get(index_url(tenk["accessionNumber"]))
        save("msft_10k_index.htm.gz", tenk_index)
        save(
            "msft_10k_primary.htm.gz",
            client.get(folder(tenk["accessionNumber"]) + tenk["primaryDocument"]),
        )

        eightk_index = client.get(index_url(eightk["accessionNumber"]))
        save("msft_8k_index.htm.gz", eightk_index)
        ex99 = doc_types(eightk_index)["EX-99.1"]
        save("msft_8k_ex99_1.htm.gz", client.get(folder(eightk["accessionNumber"]) + ex99))
    except (httpx.HTTPError, KeyError, StopIteration) as exc:
        print(f"Download failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    manifest = {
        "cik": CIK,
        "ticker": "MSFT",
        "tenk": {
            "accession": tenk["accessionNumber"],
            "filing_date": tenk["filingDate"],
            "primary_document": tenk["primaryDocument"],
        },
        "eightk": {
            "accession": eightk["accessionNumber"],
            "filing_date": eightk["filingDate"],
            "ex99_1": ex99,
        },
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    print(f"Done. Fixtures written to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
