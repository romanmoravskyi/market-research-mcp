"""Stdio smoke test: spawn the real server with `uv run market-research-mcp` and talk MCP to it.

Runs in CI (no network, no keys): it initializes, lists tools and calls `ping`. Any non-protocol
line on stdout reaches the client as an exception and fails the test; logs must go to stderr.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which("uv")

pytestmark = pytest.mark.skipif(UV is None, reason="uv is not on PATH")


def _env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env.update(
        MRM_ENV_FILE=str(tmp_path / "missing.env"),
        CACHE_PATH=str(tmp_path / "cache.db"),
        LOG_LEVEL="DEBUG",
        PYTHONUNBUFFERED="1",  # stray prints reach the client at once, not at exit
    )
    env.pop("SEC_USER_AGENT", None)
    return env


async def test_stdio_initialize_list_and_ping(tmp_path):
    params = StdioServerParameters(
        command=UV,
        args=["run", "--frozen", "--project", str(ROOT), "market-research-mcp"],
        env=_env(tmp_path),
        cwd=str(tmp_path),  # no stray .env picked up from the working directory
    )
    stray: list[Exception] = []

    async def on_message(message):
        if isinstance(message, Exception):
            stray.append(message)

    stderr_path = tmp_path / "stderr.log"
    with stderr_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write, message_handler=on_message) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "market-research"
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"ping", "resolve_ticker", "get_fundamentals"} <= tools
                prompts = {p.name for p in (await session.list_prompts()).prompts}
                assert "analyze_ticker" in prompts
                result = await session.call_tool("ping", {})
                assert not result.isError
                assert result.structuredContent
                payload = result.structuredContent.get("result", result.structuredContent)
                assert payload["status"] == "ok"

    assert not stray, f"non-protocol output on stdout: {stray!r}"
    # Logging is on (DEBUG) and lands on stderr, never stdout.
    assert "Starting market-research over stdio" in stderr_path.read_text(encoding="utf-8")
