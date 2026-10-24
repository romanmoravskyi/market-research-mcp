"""Record real MSFT Form 4 filings (XML + filing index.json) into tests/fixtures/sec/form4/.

Usage (from the repo root):
    $env:SEC_USER_AGENT = "Your Name your.email@example.com"   # PowerShell, or put it in .env
    uv run python scripts/record_form4_fixtures.py

Walks the newest MSFT Form 4 filings listed in the recorded submissions fixture
(tests/fixtures/sec/CIK0000789019.json[.gz]) and saves the newest consecutive run of filings
that together contain a sale or tax-withholding (S or F) and a derivative transaction (M),
up to MAX_FILINGS. Files are gzip-compressed. Requests are limited to 10 per second as SEC
asks. The User-Agent is read from the environment only and is never written or printed.
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
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SEC = ROOT / "tests" / "fixtures" / "sec"
OUT = SEC / "form4"
CIK = 789019
ARCHIVES = "https://www.sec.gov/Archives/edgar/data/"
MIN_INTERVAL = 0.12  # under 10 requests per second
MAX_FILINGS = 8
MIN_FILINGS = 6


def load_submissions() -> dict:
    for name in ("CIK0000789019.json", "CIK0000789019.json.gz"):
        path = SEC / name
        if path.exists():
            raw = path.read_bytes()
            if name.endswith(".gz"):
                raw = gzip.decompress(raw)
            return json.loads(raw)
    sys.exit("MSFT submissions fixture not found")


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


def codes(xml: bytes) -> set[str]:
    root = ET.fromstring(xml)
    found = {el.text.strip() for el in root.iter("transactionCode") if el.text}
    if root.find("derivativeTable/derivativeTransaction") is not None:
        found.add("deriv")
    return found


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        sys.exit("Set SEC_USER_AGENT first (environment or .env).")
    recent = load_submissions()["filings"]["recent"]
    filings = [
        (recent["accessionNumber"][i], recent["primaryDocument"][i])
        for i in range(len(recent["form"]))
        if recent["form"][i] == "4"
    ]
    client = Client(ua)
    OUT.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    saved = 0
    for accession, _primary in filings[:MAX_FILINGS]:
        folder = f"{ARCHIVES}{CIK}/{accession.replace('-', '')}/"
        index = client.get(folder + "index.json")
        items = json.loads(index)["directory"]["item"]
        xml_name = next(
            i["name"] for i in items if i["name"].lower().endswith(".xml") and "/" not in i["name"]
        )
        xml = client.get(folder + xml_name)
        (OUT / f"{accession}-index.json.gz").write_bytes(gzip.compress(index, mtime=0))
        (OUT / f"{accession}-{xml_name}.gz").write_bytes(gzip.compress(xml, mtime=0))
        seen |= codes(xml)
        saved += 1
        print(f"  {accession} {xml_name} codes={sorted(codes(xml))}")
        if saved >= MIN_FILINGS and ({"S", "F"} & seen) and "M" in seen:
            break
    print(f"saved {saved} filings, codes seen: {sorted(seen)}")


if __name__ == "__main__":
    main()
