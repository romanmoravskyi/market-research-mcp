import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sample-onepager.md"

_spec = importlib.util.spec_from_file_location(
    "check_sources", ROOT / "skills" / "analyze-ticker" / "scripts" / "check_sources.py"
)
cs = importlib.util.module_from_spec(_spec)
sys.modules["check_sources"] = cs
_spec.loader.exec_module(cs)

FOOT = "\n---\nResearch tooling output, not financial advice.\n"
SOURCES = "\n## Sources\n\n1. SEC 10-K, https://www.sec.gov/x, filed 2025-08-01\n2. No link here\n"


def doc(body: str) -> str:
    return "# Test Corp (TST)\n\n" + body + SOURCES + FOOT


def reasons(text: str) -> list[str]:
    return [str(p) for p in cs.check_text(text)]


def test_clean_fixture_passes():
    assert cs.check_text(FIXTURE.read_text(encoding="utf-8")) == []
    assert cs.main([str(FIXTURE)]) == 0


def test_unsourced_number_flagged():
    out = reasons(doc("Revenue was $12.3B in FY2025.\n"))
    assert len(out) == 1
    assert out[0].startswith("line 3:")
    assert "without source" in out[0]


@pytest.mark.parametrize("value", ["12.5%", "1,234", "$5", "3.2M", "42K"])
def test_number_formats_flagged(value):
    assert reasons(doc(f"Metric: {value}\n"))


def test_inline_url_counts_as_source():
    assert reasons(doc("Revenue $12.3B (https://www.sec.gov/y)\n")) == []


def test_footnote_resolving_works():
    assert reasons(doc("Revenue $12.3B [1]\n")) == []


def test_footnote_without_url_or_missing_flagged():
    out = reasons(doc("Revenue $12.3B [2]\nMargin 40% [9]\n"))
    assert len(out) == 2
    assert all("does not resolve" in r for r in out)


def test_banned_phrase_flagged():
    out = reasons(doc("Analysts have a Strong Buy and a price target [1].\n"))
    assert any("strong buy" in r for r in out)
    assert any("price target" in r for r in out)
    assert reasons(doc("We think shares look undervalued.\n"))
    assert reasons(doc("You should BUY this.\n"))


def test_allowed_phrases_not_flagged():
    body = (
        "Open-market buy/sell (codes P/S) shown in headline.\n"
        "P = open market or private purchase; S = open market or private sale.\n"
        "The company announced a share buyback program.\n"
    )
    assert reasons(doc(body)) == []


def test_dates_years_and_ids_not_flagged():
    body = (
        "## Fundamentals 2025\n\n"
        "Generated 2026-10-24, filed 2025-08-01T12:00:00Z.\n"
        "CIK: 0000789019, accession 0000950170-25-123456.\n"
        "Latest 10-K, Item 1A and 8-K Item 2.02 EX-99.1 reviewed for FY2025 and Q3.\n"
        "1. First risk item\n"
        "MSFT\n"
        "Price stats cover 1m, 3m, 1y and 5y windows plus the 52w range.\n"
    )
    assert reasons(doc(body)) == []


def test_sources_and_unverified_sections_skipped():
    body = "## Could not verify\n\n- UNVERIFIED: headcount 12,000\n"
    assert reasons(doc(body)) == []


def test_missing_sources_and_footer_fail():
    out = reasons("# T\n\nNothing numeric here.\n")
    assert any("missing Sources" in r for r in out)
    assert any("missing footer" in r for r in out)


def test_cli_exit_codes(tmp_path, capsys):
    bad = tmp_path / "bad.md"
    bad.write_text(doc("Revenue $1B\n"), encoding="utf-8")
    assert cs.main([str(bad)]) == 1
    assert "line 3:" in capsys.readouterr().out
    assert cs.main([str(tmp_path / "missing.md")]) == 2
    assert cs.main([]) == 2
