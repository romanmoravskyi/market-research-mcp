"""MCP prompt fallback for clients without skills: same procedure text as the skill."""

import re
from pathlib import Path

from market_research_mcp import prompts
from market_research_mcp.server import build_server

ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = ROOT / "skills" / "analyze-ticker" / "SKILL.md"


def _skill_body() -> str:
    text = SKILL_MD.read_text(encoding="utf-8")
    return re.sub(r"^---\n.*?\n---\n+", "", text, count=1, flags=re.DOTALL)


def test_procedure_matches_skill_body():
    assert prompts.analyze_ticker_procedure() == _skill_body()


def test_prompt_text_names_ticker_and_rules():
    text = prompts.analyze_ticker_text("msft")
    assert "MSFT" in text
    assert "UNVERIFIED" in text
    assert "not financial advice" in text
    assert text.endswith(prompts.analyze_ticker_procedure())


async def test_prompt_registered_with_ticker_argument():
    mcp = build_server()
    listed = {p.name: p for p in await mcp.list_prompts()}
    assert "analyze_ticker" in listed
    args = [a.name for a in listed["analyze_ticker"].arguments or []]
    assert args == ["ticker"]

    result = await mcp.get_prompt("analyze_ticker", {"ticker": "aapl"})
    text = result.messages[0].content.text
    assert "AAPL" in text
    assert "get_fundamentals" in text
