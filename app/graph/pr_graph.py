from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.graph.sdk import create_source, get_kg
from app.graph.store import clear_code_graph, delete_code_graph, write_code_graph
from app.parser.treesitter import is_supported_source_file, parse_code_file
from app.state import mark_graph_build


logger = logging.getLogger(__name__)


def pr_suffix(pr_number: int) -> str:
    return f"pr:{pr_number}"


def build_pr_graph(
    owner: str,
    repo: str,
    pr_number: int,
    changed_files: list[dict[str, str]],
    settings: Settings | None = None,
) -> int:
    settings = settings or get_settings()
    suffix = pr_suffix(pr_number)
    parsed_files = [
        parse_code_file(file["path"], file["content"])
        for file in changed_files
        if file.get("content") is not None and is_supported_source_file(file["path"])
    ]

    clear_code_graph(owner, repo, settings, suffix)
    if not parsed_files:
        logger.info("No supported PR files found for %s/%s PR #%s", owner, repo, pr_number)
        mark_graph_build(owner, repo, suffix, "empty", 0, settings)
        return 0

    write_code_graph(owner, repo, parsed_files, settings, suffix)
    kg = get_kg(owner, repo, suffix=suffix, settings=settings)
    kg.process_sources([create_source(parsed_file.source_text) for parsed_file in parsed_files])
    mark_graph_build(owner, repo, suffix, "built", len(parsed_files), settings)
    logger.info("Built PR graph for %s/%s PR #%s with %s files", owner, repo, pr_number, len(parsed_files))
    return len(parsed_files)


def delete_pr_graph(
    owner: str,
    repo: str,
    pr_number: int,
    settings: Settings | None = None,
) -> None:
    delete_code_graph(owner, repo, settings or get_settings(), pr_suffix(pr_number))
    logger.info("Deleted PR graph for %s/%s PR #%s", owner, repo, pr_number)
