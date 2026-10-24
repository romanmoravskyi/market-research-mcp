"""Fixture hygiene: recorded fixtures must not carry emails, keys, tokens or User-Agent headers.

Every file under tests/fixtures is scanned (`.gz` files are decompressed first). The only
emails allowed are reserved example domains and a short list of public role addresses that
appear verbatim in recorded SEC documents. Fixture README files may mention the User-Agent
header in their re-recording instructions; recorded data may not.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FORBIDDEN = {
    "api_key=": re.compile(rb"api_key=", re.IGNORECASE),
    "token=": re.compile(rb"token=", re.IGNORECASE),
    "User-Agent": re.compile(rb"user-agent", re.IGNORECASE),
}
# RFC 2606 reserved domains, safe for placeholders.
RESERVED_EMAIL_DOMAINS = (b"example.com", b"example.org", b"example.net")
# Public role addresses printed in recorded SEC documents (not personal data):
PUBLIC_EMAILS = {
    b"webmaster@sec.gov",  # author element of every EDGAR Atom feed
    b"rrt@we-worldwide.com",  # media-relations contact in Microsoft's public 8-K EX-99.1
}
DOC_FILES = {"README.md"}


def read_fixture(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def find_violations(name: str, data: bytes) -> list[str]:
    """Return a description of each hygiene problem in one fixture's content."""
    problems: list[str] = []
    for match in EMAIL.finditer(data):
        email = match.group(0)
        domain = email.rsplit(b"@", 1)[1].lower()
        if email.lower() in PUBLIC_EMAILS or domain in RESERVED_EMAIL_DOMAINS:
            continue
        problems.append("email address")
    for label, pattern in FORBIDDEN.items():
        if label == "User-Agent" and Path(name).name in DOC_FILES:
            continue
        if pattern.search(data):
            problems.append(label)
    return sorted(set(problems))


def fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


@pytest.mark.parametrize("path", fixture_files(), ids=lambda p: p.relative_to(FIXTURES).as_posix())
def test_fixture_is_clean(path: Path) -> None:
    # Report only the kind of problem, never the matched value.
    assert find_violations(path.name, read_fixture(path)) == []


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (b'{"contact": "jane.doe@mail.test"}', ["email address"]),
        (b"https://api.example.test/x?api_key=abc123", ["api_key="]),
        (b"https://api.example.test/x?token=abc123", ["token="]),
        (b"User-Agent: Jane Doe", ["User-Agent"]),
    ],
)
def test_detects_injected_sample(sample: bytes, expected: list[str]) -> None:
    assert find_violations("sample.json", sample) == expected


def test_allows_reserved_and_public_addresses() -> None:
    sample = b"you@example.com webmaster@sec.gov"
    assert find_violations("sample.json", sample) == []


def test_readme_may_mention_user_agent() -> None:
    assert find_violations("README.md", b"SEC requires a User-Agent") == []


def test_gzip_fixtures_are_decompressed(tmp_path: Path) -> None:
    path = tmp_path / "sample.json.gz"
    path.write_bytes(gzip.compress(b"token=abc"))
    assert find_violations(path.name, read_fixture(path)) == ["token="]
