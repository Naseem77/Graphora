"""MCP stdio server: expose the code graph to any AI agent.

Tools:
- graph_stats: node/edge counts for the project graph
- blast_radius: impact of changing symbols (callers/callees/tests + risk)
- review_diff: grounded facts-mode review of a unified diff
- risk_top: riskiest symbols from git history mining
- find_symbol: locate a symbol definition

Run: graphora serve-mcp --project <name>
or:  python -m graphora.mcp_server <project>
"""

from __future__ import annotations

import json
import sys

from graphora.store import open_store

_INSTRUCTIONS = (
    "Graphora exposes a deterministic code knowledge graph (built with "
    "tree-sitter, no LLM) plus a risk memory mined from git history. "
    "Before editing or reviewing code, call blast_radius to see callers, "
    "callees, tests, and historical breakage for the symbols you touch."
)


def build_server(project: str, host: str = "localhost", port: int = 6379, backend: str = "auto"):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("graphora", instructions=_INSTRUCTIONS)
    store = open_store(project, backend=backend, host=host, port=port)

    @mcp.tool()
    def graph_stats() -> str:
        """Node and edge counts for the indexed project graph."""
        return json.dumps(store.stats())

    @mcp.tool()
    def blast_radius(symbols: list[str]) -> str:
        """Impact of changing the given symbols: callers, callees, covering
        tests, importers, and risk memory. Each relation is confidence-tagged
        (EXTRACTED, INFERRED, or AMBIGUOUS)."""
        from graphora.blast import blast_radius as _blast

        return json.dumps(_blast(store, symbols).to_dict())

    @mcp.tool()
    def review_diff(diff: str) -> str:
        """Grounded review of a unified diff: blast radius facts, missing-test
        warnings, and risk-memory alerts. Deterministic, no LLM."""
        from graphora.review import review_diff as _review

        return _review(store, diff, use_llm=False).render()

    @mcp.tool()
    def risk_top(limit: int = 15) -> str:
        """The riskiest symbols in the codebase, ranked by risk score
        (fix/revert frequency decayed by recency), with caller counts."""
        from graphora.risk import risk_report

        return json.dumps(risk_report(store, limit=limit))

    @mcp.tool()
    def find_symbol(name: str) -> str:
        """Locate a function or class definition by name."""
        return json.dumps(store.find_symbol(name))

    return mcp


def serve(project: str, host: str = "localhost", port: int = 6379, backend: str = "auto") -> None:
    build_server(project, host=host, port=port, backend=backend).run(transport="stdio")


if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else "default")
