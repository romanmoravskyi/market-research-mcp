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


# Review fixes (PR #6). Each case named in the review.


@pytest.mark.parametrize(
    "line",
    [
        "Revenue $3m in the quarter.",  # finding 2
        "Revenue $12m in the quarter.",  # finding 2
        "Q2 42% gross margin.",  # finding 3
        "H1 120 stores opened.",  # finding 3
        "Insiders sold 2000 shares.",  # finding 4
        "DSO 45 days",  # finding 4
    ],
)
def test_review_unsourced_numbers_flagged(line):
    out = reasons(doc(line + "\n"))
    assert len(out) == 1, out
    assert "without source" in out[0]


@pytest.mark.parametrize(
    "line",
    [
        "Q3 FY2025, Q4 2025 and Q3'25 were reviewed.",
        "Revenue grew in 2025 and since 2021.",
        "Results for fiscal 2024 and December 2025 are included.",
        "Insider activity over the last 12 months (12 months window).",
        "None found within 120 days.",
        "## Insiders (12 months)",
    ],
)
def test_review_period_labels_still_not_flagged(line):
    assert reasons(doc(line + "\n")) == []


def test_review_template_header_with_sic_passes():  # finding 6
    line = "CIK: 0000999999 | Exchange: NASDAQ | SIC: 7372 | Generated: 2026-10-24\n"
    assert reasons(doc(line)) == []


@pytest.mark.parametrize(
    "line",
    [
        "# Best Buy Co., Inc. (BBY) - research brief",  # finding 5
        "Buy-side analysts attended the call.",
        "Open-market buys: 2 purchases [1].",
        "The company announced a share buyback program.",
        "Insiders sell shares under 10b5-1 plans.",
    ],
)
def test_review_company_names_and_neutral_words_pass(line):
    assert reasons(doc(line + "\n")) == []


@pytest.mark.parametrize(
    ("line", "phrase"),
    [
        ("Analysts have a Strong Buy on it.", "strong buy"),
        ("It carries a buy rating.", "buy rating"),
        ("Rating: outperform.", "outperform"),
        ("Shares were upgraded to Overweight.", "overweight"),
        ("Moved to underweight.", "underweight"),
        ("Hold rating maintained.", "hold rating"),
        ("Sell rating issued.", "sell rating"),
        ("We recommend buying the shares.", "we recommend buying"),
        ("The price target is unchanged.", "price target"),
        ("Shares look undervalued.", "undervalued"),
    ],
)
def test_review_recommendation_phrases_flagged(line, phrase):  # findings 5 and 7
    out = reasons(doc(line + "\n"))
    assert any(f"'{phrase}'" in r for r in out), out


def test_review_every_unresolved_ref_reported():  # finding 8
    out = reasons(doc("Revenue $1B [1], margin 40% [9]\n"))
    assert len(out) == 1, out
    assert "[9]" in out[0]
    assert "[1]" not in out[0]


# End-to-end run (T13): false positives on lines the skill itself asks for.


@pytest.mark.parametrize(
    "line",
    [
        "## Insiders (Form 4 filings 2026-09-01 to 2026-10-15)",  # ISO dates in a heading
        "Fiscal year ends June 30.",  # the skill asks to state a non-December year end
        "Fiscal year ends September 30 (fiscal 2026 = October 2025 to September 2026).",
        "Q4 flows marked derived are FY minus 9M YTD.",  # the tool's own period label
        "Quarters use the 6M YTD and 9M YTD facts.",
    ],
)
def test_e2e_dates_and_period_labels_not_flagged(line):
    assert reasons(doc(line + "\n")) == []


@pytest.mark.parametrize(
    "line",
    [
        "## Revenue $12.3B (2026-06-30)",  # a heading with a number still needs a source
        "Revenue June 30 was $9M.",
        "Cash of $9M YTD.",
    ],
)
def test_e2e_money_next_to_dates_still_flagged(line):
    out = reasons(doc(line + "\n"))
    assert len(out) == 1, out


# PR #9 review: exempt only exact date or period tokens, never a number next to a unit.


@pytest.mark.parametrize(
    "line",
    [
        "Revenue June 30 bn",  # finding 1
        "Buyback in May 15 million shares",  # finding 1
        "June 30 bn",
        "Opex of €9M YTD",  # finding 6
        "Net loss 3M YTD",  # finding 6
        "Revenue 9M YTD",
    ],
)
def test_review9_amounts_next_to_date_or_period_tokens_flagged(line):
    out = reasons(doc(line + "\n"))
    assert len(out) == 1, out
    assert "without source" in out[0]


@pytest.mark.parametrize(
    "line",
    [
        "## Revenue 2021-2025",  # finding 2
        "## Revenue 2021 - 2025 (FY2021 to FY2025)",
        "Fiscal year ends June 30, per the 10-K.",
        "Derived quarters (9M YTD) are FY minus the 9M YTD figure.",
    ],
)
def test_review9_exact_date_and_period_tokens_pass(line):
    assert reasons(doc(line + "\n")) == []
