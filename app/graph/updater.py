from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.graph.locks import graph_write_lock
from app.graph.sdk import create_source, get_kg
from app.graph.store import delete_file_subgraph, write_code_graph
from app.parser.treesitter import is_supported_source_file, parse_code_file
from app.state import mark_graph_build


logger = logging.getLogger(__name__)


def update_graph_for_push(owner: str, repo: str, changed_files: list[dict[str, str]], settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    parsed_files = [
        parse_code_file(file["path"], file["content"])
        for file in changed_files
        if file.get("content") is not None and is_supported_source_file(file["path"])
    ]
    if not parsed_files:
        return 0

    with graph_write_lock(owner, repo, "main", settings):
        _delete_stale_file_nodes(owner, repo, changed_files, settings)
        write_code_graph(owner, repo, parsed_files, settings)

        kg = get_kg(owner, repo, settings=settings)
        sources = [create_source(parsed_file.source_text) for parsed_file in parsed_files]
        kg.process_sources(sources)
        mark_graph_build(owner, repo, "main", "updated", len(sources), settings)
    logger.info("Updated graph for %s/%s with %s changed files", owner, repo, len(sources))
    return len(sources)


def _delete_stale_file_nodes(
    owner: str,
    repo: str,
    changed_files: list[dict[str, str]],
    settings: Settings,
) -> None:
    for file in changed_files:
        path = file["path"]
        delete_file_subgraph(owner, repo, path, settings)
