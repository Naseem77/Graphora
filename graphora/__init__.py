"""Graphora: a deterministic code knowledge-graph tool.

Index a repository into FalkorDB with tree-sitter (no LLM), query blast
radius for any change, mine git history into a risk memory, and review
diffs grounded in real structure. Usable as a library, CLI, or MCP server.
"""

from __future__ import annotations

import importlib

__version__ = "0.2.1"

_LAZY = {
    "parse_code_file": "graphora.parser",
    "extract_symbols_from_diff": "graphora.parser",
    "ParsedFile": "graphora.parser",
    "GraphStore": "graphora.store",
    "open_store": "graphora.store",
    "EmbeddedGraphStore": "graphora.embedded",
    "index_repository": "graphora.indexer",
    "update_files": "graphora.indexer",
    "blast_radius": "graphora.blast",
    "BlastRadius": "graphora.blast",
    "mine_risk_memory": "graphora.risk",
    "risk_report": "graphora.risk",
}

__all__ = list(_LAZY) + ["__version__"]


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module 'graphora' has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
