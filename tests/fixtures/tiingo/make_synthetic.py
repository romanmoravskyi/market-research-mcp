"""Generate SYNTHETIC Tiingo-shaped fixtures for MSFT. The prices are made up.

Tiingo data may not be redistributed, so these fixtures only copy the JSON shape of
`/tiingo/daily/<ticker>/prices` and `/tiingo/daily/<ticker>`, never real values.

Run from the repo root: `uv run python tests/fixtures/tiingo/make_synthetic.py`
The output is deterministic (fixed seed), so re-running gives identical files.
"""

from __future__ import annotations

import gzip
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
SEED = 20261024
START = date(2020, 10, 26)
END = date(2026, 10, 23)  # a Friday
SPLIT_DATE = date(2023, 3, 15)  # synthetic 4:1 split
SPLIT_FACTOR = 4.0


def weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def build() -> list[dict]:
    rng = random.Random(SEED)
    adj = 50.0  # split-adjusted close
    rows = []
    for d in weekdays(START, END):
        prev = adj
        adj = prev * math.exp(rng.gauss(0.0004, 0.015))
        a_open = prev * math.exp(rng.gauss(0, 0.004))
        a_high = max(a_open, adj) * (1 + abs(rng.gauss(0, 0.006)))
        a_low = min(a_open, adj) * (1 - abs(rng.gauss(0, 0.006)))
        a_vol = rng.randint(4_000_000, 12_000_000)
        factor = SPLIT_FACTOR if d < SPLIT_DATE else 1.0  # raw = adjusted * factor
        rows.append(
            {
                "date": f"{d.isoformat()}T00:00:00.000Z",
                "open": round(a_open * factor, 4),
                "high": round(a_high * factor, 4),
                "low": round(a_low * factor, 4),
                "close": round(adj * factor, 4),
                "volume": int(a_vol / factor),
                "adjOpen": round(a_open, 4),
                "adjHigh": round(a_high, 4),
                "adjLow": round(a_low, 4),
                "adjClose": round(adj, 4),
                "adjVolume": a_vol,
                "divCash": 0.0,
                "splitFactor": SPLIT_FACTOR if d == SPLIT_DATE else 1.0,
            }
        )
    return rows


def main() -> None:
    rows = build()
    payload = json.dumps(rows, separators=(",", ":")).encode()
    (HERE / "msft_prices.json.gz").write_bytes(gzip.compress(payload, mtime=0))
    meta = {
        "ticker": "MSFT",
        "name": "Synthetic Test Corp (fake data)",
        "exchangeCode": "NASDAQ",
        "startDate": rows[0]["date"][:10],
        "endDate": rows[-1]["date"][:10],
        "description": "Synthetic fixture. Not real market data.",
    }
    (HERE / "msft_meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"{len(rows)} bars, {len(payload)} bytes uncompressed")


if __name__ == "__main__":
    main()
