from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.graph.sdk import graph_name
from app.graph.store import _select_graph


@dataclass(frozen=True)
class GraphDiff:
    added_files: list[str]
    changed_files: list[str]
    added_symbols: list[str]
    changed_symbols: list[str]
    removed_symbols: list[str]

    def to_prompt_context(self) -> str:
        return "\n".join(
            [
                "Structured graph diff:",
                f"- Added files: {', '.join(self.added_files) or 'none'}",
                f"- Changed files: {', '.join(self.changed_files) or 'none'}",
                f"- Added symbols: {', '.join(self.added_symbols) or 'none'}",
                f"- Changed symbols: {', '.join(self.changed_symbols) or 'none'}",
                f"- Removed symbols: {', '.join(self.removed_symbols) or 'none'}",
            ]
        )


def diff_main_vs_pr(owner: str, repo: str, pr_number: int, settings: Settings | None = None) -> GraphDiff:
    settings = settings or get_settings()
    main_graph = _select_graph(owner, repo, settings)
    pr_graph = _select_graph(owner, repo, settings, f"pr:{pr_number}")

    main_files = _keyed_rows(main_graph, "MATCH (f:File) RETURN f.path, f.content_hash")
    pr_files = _keyed_rows(pr_graph, "MATCH (f:File) RETURN f.path, f.content_hash")
    main_symbols = _keyed_rows(main_graph, "MATCH (s) WHERE s:Function OR s:Class RETURN s.stable_key, s.signature_hash")
    pr_symbols = _keyed_rows(pr_graph, "MATCH (s) WHERE s:Function OR s:Class RETURN s.stable_key, s.signature_hash")

    return GraphDiff(
        added_files=sorted(path for path in pr_files if path not in main_files),
        changed_files=sorted(path for path, digest in pr_files.items() if path in main_files and main_files[path] != digest),
        added_symbols=sorted(key for key in pr_symbols if key not in main_symbols),
        changed_symbols=sorted(key for key, digest in pr_symbols.items() if key in main_symbols and main_symbols[key] != digest),
        removed_symbols=sorted(key for key in main_symbols if _symbol_path(key) in pr_files and key not in pr_symbols),
    )


def _keyed_rows(graph: object, query: str) -> dict[str, str]:
    result = graph.query(query)
    rows = getattr(result, "result_set", result)
    values: dict[str, str] = {}
    for row in rows or []:
        if len(row) >= 2 and row[0]:
            values[str(row[0])] = str(row[1] or "")
    return values


def _symbol_path(stable_key: str) -> str:
    return stable_key.split(":Function:", 1)[0].split(":Class:", 1)[0]
