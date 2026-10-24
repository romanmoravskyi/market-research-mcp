"""Claude Code plugin and marketplace manifests: valid JSON, required fields, no secrets.

Format per https://code.claude.com/docs/en/plugins-reference and
https://code.claude.com/docs/en/plugins/marketplace-reference.md
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = ROOT / ".claude-plugin" / "marketplace.json"
SKILL_MD = ROOT / "skills" / "analyze-ticker" / "SKILL.md"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_plugin_json_required_fields():
    plugin = _load(PLUGIN_JSON)
    assert NAME_RE.match(plugin["name"])
    assert not plugin["name"].startswith(("claude-", "anthropic-"))
    for field in ("version", "description", "license"):
        assert plugin[field]
    assert plugin["author"]["name"]


def test_plugin_wires_mcp_server_via_plugin_root():
    servers = _load(PLUGIN_JSON)["mcpServers"]
    server = servers["market-research"]
    assert server["command"] == "uv"
    args = server["args"]
    assert "${CLAUDE_PLUGIN_ROOT}" in args
    assert args[-1] == "market-research-mcp"


def test_plugin_has_no_literal_secrets():
    server = _load(PLUGIN_JSON)["mcpServers"]["market-research"]
    for value in server.get("env", {}).values():
        assert re.fullmatch(r"\$\{[A-Z_]+(:-[^}]*)?\}", value), value


def test_marketplace_json_lists_this_plugin():
    market = _load(MARKETPLACE_JSON)
    plugin = _load(PLUGIN_JSON)
    assert NAME_RE.match(market["name"])
    assert market["owner"]["name"]
    assert market["description"]
    assert len(market["plugins"]) == 1
    entry = market["plugins"][0]
    assert entry["name"] == plugin["name"]
    assert entry["source"] == "./"
    assert entry["description"]


def test_skill_frontmatter_and_checker_path():
    text = SKILL_MD.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md needs YAML frontmatter"
    front = m.group(1)
    assert re.search(r"^name: analyze-ticker$", front, re.MULTILINE)
    assert re.search(r"^description: \S", front, re.MULTILINE)
    rel = "skills/analyze-ticker/scripts/check_sources.py"
    assert rel in text
    assert "${CLAUDE_PLUGIN_ROOT}" in text
    assert (ROOT / rel).is_file()


def test_skill_keeps_hard_rules():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "UNVERIFIED" in text
    assert "not financial advice" in text
    assert "price targets" in text
    assert "JOURNAL_DIR" in text
