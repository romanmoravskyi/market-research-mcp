"""Shared test helpers."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

SEC_FIXTURES = Path(__file__).parent / "fixtures" / "sec"


def load_sec_fixture(name: str) -> Any:
    """Load a JSON fixture from tests/fixtures/sec/ (subpaths allowed).

    `name` may be given with or without `.gz`; `<name>` is tried first, then `<name>.gz`.
    """
    path = SEC_FIXTURES / name
    if not path.exists() and not name.endswith(".gz"):
        path = SEC_FIXTURES / f"{name}.gz"
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return json.loads(raw)
