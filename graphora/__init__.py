"""Graphora: know what breaks before you change it.

Index every function, method, and class of a repository with tree-sitter
(no LLM) into FalkorDB or an embedded store, then ask what a change could
break: callers that will break, callers that might (with the alternatives
each uncertain call could mean), and the tests that reach it. Usable as a
library, CLI, or MCP server.
"""

from __future__ import annotations

import importlib

__version__ = "0.3.1"

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
}

__all__ = list(_LAZY) + ["__version__"]


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module 'graphora' has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
