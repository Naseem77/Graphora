from __future__ import annotations

import logging
from typing import Callable

from app.config import Settings, get_settings
from app.graph.locks import graph_write_lock
from app.graph.store import clear_code_graph, delete_code_graph, write_code_graph
from app.parser.treesitter import is_supported_source_file, parse_code_file
from app.state import mark_graph_build


logger = logging.getLogger(__name__)
ProgressCallback = Callable[[str, int, int, str], None]


def pr_suffix(pr_number: int) -> str:
    return f"pr:{pr_number}"


def build_pr_graph(
    owner: str,
    repo: str,
    pr_number: int,
    changed_files: list[dict[str, str]],
    settings: Settings | None = None,
    on_progress: ProgressCallback | None = None,
) -> int:
    settings = settings or get_settings()
    suffix = pr_suffix(pr_number)
    supported_files = [
        file
        for file in changed_files
        if file.get("content") is not None and is_supported_source_file(file["path"])
    ]
    total = len(supported_files)
    parsed_files = []
    for index, file in enumerate(supported_files, start=1):
        _report_progress(on_progress, "parsing", index, total, file["path"])
        parsed_files.append(parse_code_file(file["path"], file["content"]))

    with graph_write_lock(owner, repo, suffix, settings):
        clear_code_graph(owner, repo, settings, suffix)
        if not parsed_files:
            logger.info("No supported PR files found for %s/%s PR #%s", owner, repo, pr_number)
            mark_graph_build(owner, repo, suffix, "empty", 0, settings)
            return 0

        for index, parsed_file in enumerate(parsed_files, start=1):
            write_code_graph(owner, repo, [parsed_file], settings, suffix)
            _report_progress(on_progress, "writing structural graph", index, total, parsed_file.path)
        mark_graph_build(owner, repo, suffix, "built", len(parsed_files), settings)
    _report_progress(on_progress, "complete", total, total, "")
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


def _report_progress(
    on_progress: ProgressCallback | None,
    stage: str,
    current: int,
    total: int,
    path: str,
) -> None:
    if on_progress is not None:
        on_progress(stage, current, total, path)
