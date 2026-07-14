"""Phase 5 tests: MCP server tools (integration, needs FalkorDB + mcp)."""

import json
from pathlib import Path

import pytest

pytest.importorskip("falkordb")
pytest.importorskip("mcp")
from falkordb import FalkorDB  # noqa: E402

from graphora.indexer import index_repository  # noqa: E402
from graphora.mcp_server import build_server  # noqa: E402
from graphora.store import GraphStore  # noqa: E402


def _falkordb_available() -> bool:
    try:
        FalkorDB(host="localhost", port=6379).select_graph("graphora:ping").query("RETURN 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _falkordb_available(), reason="FalkorDB not running on localhost:6379")


@pytest.fixture()
def project(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def compute(x):\n    return x + 1\n\ndef pipeline(x):\n    return compute(x)\n"
    )
    (tmp_path / "tests" / "test_core.py").write_text(
        "from pkg.core import compute\n\ndef test_compute():\n    assert compute(1) == 2\n"
    )
    name = "graphora-mcp-test"
    index_repository(tmp_path, project=name)
    yield name
    GraphStore(name).delete_graph()


@pytest.fixture()
def server(project):
    return build_server(project)


@pytest.mark.anyio
async def test_mcp_lists_all_tools(server):
    tools = await server.list_tools()
    names = {tool.name for tool in tools}
    assert names == {"graph_stats", "blast_radius", "review_diff", "risk_top", "find_symbol"}


@pytest.mark.anyio
async def test_mcp_blast_radius_tool(server):
    result = await server.call_tool("blast_radius", {"symbols": ["compute"]})
    payload = json.loads(result[0][0].text)
    assert payload["symbols"][0]["name"] == "compute"
    assert any(c["name"] == "pipeline" for c in payload["symbols"][0]["callers"])
    assert any(t["name"] == "test_compute" for t in payload["symbols"][0]["tests"])


@pytest.mark.anyio
async def test_mcp_find_symbol_and_stats(server):
    result = await server.call_tool("find_symbol", {"name": "pipeline"})
    hits = json.loads(result[0][0].text)
    assert hits[0]["path"] == "pkg/core.py"
    result = await server.call_tool("graph_stats", {})
    stats = json.loads(result[0][0].text)
    assert stats["files"] == 2


@pytest.mark.anyio
async def test_mcp_review_diff_tool(server):
    diff = "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,2 +1,2 @@\n-def compute(x):\n+def compute(x, y=0):\n"
    result = await server.call_tool("review_diff", {"diff": diff})
    text = result[0][0].text
    assert "# Graphora Review" in text
    assert "compute" in text


@pytest.fixture()
def anyio_backend():
    return "asyncio"
