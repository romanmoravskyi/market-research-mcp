"""Server startup: native libraries load before the stdio transport starts.

On Windows, loading numpy's DLLs for the first time while the stdio transport has a blocking
read pending on stdin deadlocks: get_filing_section (edgartools imports numpy lazily in a
worker thread) hung until the client gave up. main() must import numpy before mcp.run().
"""

import os
import subprocess
import sys

PROBE = """
import sys
from mcp.server.fastmcp import FastMCP
from market_research_mcp import server
assert "numpy" not in sys.modules, "probe needs a process without numpy loaded"
def fake_run(self, transport="stdio", **kw):
    print("numpy loaded before run:", "numpy" in sys.modules)
FastMCP.run = fake_run
server.main()
"""


def test_main_imports_numpy_before_stdio_transport(tmp_path):
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
    assert "numpy loaded before run: True" in out.stdout
