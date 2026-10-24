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


OPTIONAL_TOOLS = {"get_key_ratios"}  # added by a parallel PR; the skill has a fallback
CALL_RE = re.compile(r"`([a-z_]+)\((.*?)\)`", re.DOTALL)


def _split_args(arg_text: str) -> list[str]:
    parts, depth, current, quote = [], 0, "", None
    for ch in arg_text:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


async def test_skill_tool_calls_match_server_signatures():
    tools = {t.name: t.inputSchema for t in await build_server().list_tools()}
    calls = CALL_RE.findall(SKILL_MD.read_text(encoding="utf-8"))
    assert calls
    for name, arg_text in calls:
        if name in OPTIONAL_TOOLS:
            continue
        assert name in tools, f"{name} is not a server tool"
        params = list(tools[name]["properties"])
        required = set(tools[name].get("required", []))
        args = _split_args(arg_text)
        positional = [a for a in args if "=" not in a.split("(")[0]]
        keywords = {a.split("=", 1)[0].strip() for a in args if a not in positional}
        assert len(positional) <= len(params), f"{name}: too many arguments"
        assert keywords <= set(params), f"{name}: unknown keywords {keywords - set(params)}"
        covered = set(params[: len(positional)]) | keywords
        assert required <= covered, f"{name}: missing required {required - covered}"


def test_skill_ratios_step_has_fallback():
    body = _skill_body()
    assert "get_key_ratios" in body
    assert "get_fundamentals" in body.split("get_key_ratios", 1)[1].split("\n4.", 1)[0]
    assert "formula" in body
