"""Index a local directory into a Graphora graph. Deterministic, no LLM."""

from __future__ import annotations

import fnmatch
import logging
from pathlib import Path
from typing import Callable

from graphora.parser import is_supported_code_file, parse_code_file
from graphora.store import GraphStore

logger = logging.getLogger(__name__)

DEFAULT_IGNORES = [
    ".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
    ".tox", ".mypy_cache", ".pytest_cache", "vendor", ".idea", ".vscode",
]

ProgressCallback = Callable[[str, int, int, str], None]


def collect_source_files(root: str | Path, ignores: list[str] | None = None) -> list[Path]:
    root = Path(root).resolve()
    ignore_names = set(DEFAULT_IGNORES) | set(ignores or [])
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not is_supported_code_file(str(path)):
            continue
        relative_parts = path.relative_to(root).parts
        if any(part in ignore_names for part in relative_parts):
            continue
        if any(fnmatch.fnmatch(str(path.relative_to(root)), pat) for pat in ignore_names if "*" in pat):
            continue
        files.append(path)
    return files


def index_repository(
    root: str | Path,
    project: str | None = None,
    host: str = "localhost",
    port: int = 6379,
    ignores: list[str] | None = None,
    store: GraphStore | None = None,
    on_progress: ProgressCallback | None = None,
) -> GraphStore:
    """Parse every supported file under `root` and build the graph.

    Returns the GraphStore for further queries. Zero LLM calls.
    """
    root = Path(root).resolve()
    project = project or root.name
    store = store or GraphStore(project, host=host, port=port)

    files = collect_source_files(root, ignores)
    total = len(files)
    parsed_files = []
    for index, path in enumerate(files, start=1):
        rel = str(path.relative_to(root))
        if on_progress:
            on_progress("parsing", index, total, rel)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Skipping unreadable file %s: %s", rel, exc)
            continue
        parsed_files.append(parse_code_file(rel, content))

    store.clear()
    for index, parsed in enumerate(parsed_files, start=1):
        if on_progress:
            on_progress("writing", index, total, parsed.path)
        store.write_files([parsed])

    if on_progress:
        on_progress("linking", total, total, "cross-file calls")
    linked = store.link_cross_file_calls()
    logger.info("Indexed %s files (%s cross-file call edges) into %s", len(parsed_files), linked, store.graph_name)
    return store


def update_files(
    root: str | Path,
    paths: list[str],
    project: str | None = None,
    host: str = "localhost",
    port: int = 6379,
    store: GraphStore | None = None,
) -> GraphStore:
    """Incremental update: re-parse only the given relative paths."""
    root = Path(root).resolve()
    project = project or root.name
    store = store or GraphStore(project, host=host, port=port)
    for rel in paths:
        full = root / rel
        if not full.exists():
            store.delete_file(rel)
            continue
        if not is_supported_code_file(rel):
            continue
        content = full.read_text(encoding="utf-8", errors="replace")
        store.write_files([parse_code_file(rel, content)])
    store.link_cross_file_calls()
    return store
