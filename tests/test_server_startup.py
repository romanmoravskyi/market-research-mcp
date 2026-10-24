"""Server startup: lazily imported native libraries load before the stdio transport starts.

Root cause (Windows): the stdio transport keeps a synchronous blocking read pending on the
stdin pipe in a worker thread. Loading a native extension DLL for the first time runs that
DLL's C runtime start-up, which queries the standard handles; on a synchronous pipe handle
that call waits behind the pending read, so the load blocks until the client sends another
message, which it never does while it waits for the reply. A thread dump of the hung server
showed the worker thread inside numpy's extension-module load (edgartools imported lazily
in asyncio.to_thread) and the main thread idle in the event loop, so it is not the import
lock. Importing in another thread would not help; it must happen before the transport
starts, so main() imports every module the tools import lazily.
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

from market_research_mcp import server

SRC = Path(__file__).resolve().parents[1] / "src" / "market_research_mcp"

PROBE = """
import sys
from mcp.server.fastmcp import FastMCP
from market_research_mcp import server
assert "numpy" not in sys.modules, "probe needs a process without numpy loaded"
def fake_run(self, transport="stdio", **kw):
    loaded = [m for m in server.PRELOAD_MODULES if m in sys.modules]
    print("loaded before run:", ",".join(sorted(loaded)))
FastMCP.run = fake_run
server.main()
"""


def _lazy_third_party_imports() -> set[str]:
    """Modules imported inside functions anywhere in the package (not our own)."""
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    found.add(node.module)
                elif isinstance(node, ast.Import):
                    found.update(a.name for a in node.names)
    return {m for m in found if not m.startswith("market_research_mcp")}


def test_every_lazy_import_is_preloaded():
    assert _lazy_third_party_imports() <= set(server.PRELOAD_MODULES)


def test_main_preloads_before_stdio_transport(tmp_path):
    env = {
        **os.environ,
        "MRM_ENV_FILE": str(tmp_path / "missing.env"),
        "CACHE_PATH": str(tmp_path / "cache.sqlite"),
    }
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = out.stdout.split("loaded before run:", 1)[1].strip().split(",")
    assert {"edgar.company_reports", "edgar.documents"} <= set(loaded)
