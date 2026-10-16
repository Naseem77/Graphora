"""MCP stdio server: expose the code graph to any AI agent.

Tools:
- what_could_break: callers a change can break, certain vs "X or Y"
- graph_stats: node/edge counts for the project graph
- blast_radius: impact of changing symbols (callers/callees/tests)
- review_diff: grounded facts-mode review of a unified diff
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
    "tree-sitter, no LLM). Before editing a function, method, or class, call "
    "what_could_break with its name (or what_could_break_for_diff with your "
    "diff). It separates callers that will break from callers that might, and "
    "for each uncertain call it lists what the call could mean (X or Y) instead "
    "of guessing. Report uncertain cases as such."
)


def _new_server():
    """Create the MCP server object on either SDK major version.

    mcp 2.x renamed FastMCP to MCPServer; mcp 1.x only has FastMCP.
    """
    from graphora import __version__

    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        server = FastMCP("graphora", instructions=_INSTRUCTIONS)
        # FastMCP 1.x has no version argument; report Graphora's, not the SDK's.
        low_level = getattr(server, "_mcp_server", None)
        if low_level is not None and hasattr(low_level, "version"):
            low_level.version = __version__
        return server
    return MCPServer("graphora", instructions=_INSTRUCTIONS, version=__version__)


def build_server(project: str, host: str = "localhost", port: int = 6379, backend: str = "auto"):
    mcp = _new_server()
    store = open_store(project, backend=backend, host=host, port=port)
    if not store.has_files():
        print(
            f"graphora: warning: graph {store.graph_name!r} is empty; index the repository first",
            file=sys.stderr,
        )

    @mcp.tool()
    def what_could_break(symbols: list[str], depth: int = 2, as_json: bool = False) -> str:
        """What could break if these symbols change. Accepts names,
        qualified names, or file-qualified names to pick one of several
        same-named definitions (e.g. "save", "Db.save",
        "app/db.py:Db.save"). Walks callers up to
        `depth` hops and returns: callers that WILL break (every call on the
        path is certain), callers that MIGHT break (some call is ambiguous;
        each lists the alternatives it could mean: "X or Y", possibly a
        method outside the repo), and tests that reach the change."""
        from graphora.impact import what_could_break as _what

        result = _what(store, symbols, depth=depth)
        return json.dumps(result.to_dict()) if as_json else result.to_text()

    @mcp.tool()
    def what_could_break_for_diff(diff: str, depth: int = 2, as_json: bool = False) -> str:
        """Same as what_could_break, for every function, method, or class a
        unified diff touches (edits inside a body count)."""
        from graphora.impact import what_could_break_for_diff as _what_diff

        result = _what_diff(store, diff, depth=depth)
        return json.dumps(result.to_dict()) if as_json else result.to_text()

    @mcp.tool()
    def graph_stats() -> str:
        """Node and edge counts for the indexed project graph."""
        return json.dumps(store.stats())

    @mcp.tool()
    def blast_radius(symbols: list[str]) -> str:
        """Impact of changing the given symbols: callers, callees, covering
        tests, and importers. Each relation is confidence-tagged
        (EXTRACTED, INFERRED, or AMBIGUOUS)."""
        from graphora.blast import blast_radius as _blast

        return json.dumps(_blast(store, symbols).to_dict())

    @mcp.tool()
    def review_diff(diff: str) -> str:
        """Grounded review of a unified diff: blast radius facts, missing-test
        warnings. Deterministic, no LLM."""
        from graphora.review import review_diff as _review

        return _review(store, diff, use_llm=False).render()

    @mcp.tool()
    def find_symbol(name: str) -> str:
        """Locate a function or class definition by name."""
        return json.dumps(store.find_symbol(name))

    return mcp


def serve(project: str, host: str = "localhost", port: int = 6379, backend: str = "auto") -> None:
    build_server(project, host=host, port=port, backend=backend).run(transport="stdio")


if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else "default")
