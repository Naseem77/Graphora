from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.graph.sdk import create_source, get_kg, graph_name
from app.parser.treesitter import is_supported_code_file, parse_code_file


logger = logging.getLogger(__name__)


def update_graph_for_push(owner: str, repo: str, changed_files: list[dict[str, str]], settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    _delete_stale_file_nodes(owner, repo, changed_files, settings)

    kg = get_kg(owner, repo, settings=settings)
    sources = [
        create_source(parse_code_file(file["path"], file["content"]).source_text)
        for file in changed_files
        if file.get("content") is not None and is_supported_code_file(file["path"])
    ]
    if not sources:
        return 0
    kg.process_sources(sources)
    logger.info("Updated graph for %s/%s with %s changed files", owner, repo, len(sources))
    return len(sources)


def _delete_stale_file_nodes(
    owner: str,
    repo: str,
    changed_files: list[dict[str, str]],
    settings: Settings,
) -> None:
    try:
        from falkordb import FalkorDB
    except ImportError as exc:
        raise RuntimeError("falkordb is required for incremental graph updates") from exc

    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    graph = db.select_graph(graph_name(owner, repo))
    for file in changed_files:
        path = file["path"]
        graph.query(
            "MATCH (fn)-[:DEFINED_IN]->(file:File {path: $path}) DETACH DELETE fn",
            {"path": path},
        )
        graph.query("MATCH (f:File {path: $path}) DETACH DELETE f", {"path": path})
