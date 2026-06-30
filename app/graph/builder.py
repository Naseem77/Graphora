from __future__ import annotations

import logging
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from github import Github

from app.config import Settings
from app.graph.locks import graph_write_lock
from app.graph.store import clear_code_graph, write_code_graph
from app.state import mark_graph_build
from app.parser.treesitter import is_supported_source_file, parse_code_file


logger = logging.getLogger(__name__)


RepoFile = dict[str, str]
ProgressCallback = Callable[[str, int, int, str], None]


def build_repo_graph(
    owner: str,
    repo: str,
    files: list[RepoFile],
    on_progress: ProgressCallback | None = None,
    settings: Settings | None = None,
) -> int:
    supported_files = [file for file in files if is_supported_source_file(file["path"])]
    total = len(supported_files)
    parsed_files = []
    for index, file in enumerate(supported_files, start=1):
        _report_progress(on_progress, "parsing", index, total, file["path"])
        parsed_files.append(parse_code_file(file["path"], file["content"]))
    if not parsed_files:
        logger.info("No supported code files found for %s/%s", owner, repo)
        return 0

    with graph_write_lock(owner, repo, "main", settings):
        clear_code_graph(owner, repo)
        for index, parsed_file in enumerate(parsed_files, start=1):
            write_code_graph(owner, repo, [parsed_file])
            _report_progress(on_progress, "writing structural graph", index, total, parsed_file.path)
        mark_graph_build(owner, repo, "main", "built", len(parsed_files))
    _report_progress(on_progress, "complete", total, total, "")
    logger.info("Built graph for %s/%s with %s source files", owner, repo, len(parsed_files))
    return len(parsed_files)


def fetch_repository_files(github_client: "Github", owner: str, repo: str, ref: str | None = None) -> list[RepoFile]:
    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    queue = list(repo_obj.get_contents("", ref=ref))
    files: list[RepoFile] = []

    while queue:
        item = queue.pop(0)
        if item.type == "dir":
            queue.extend(repo_obj.get_contents(item.path, ref=ref))
            continue
        if not is_supported_source_file(item.path):
            continue
        content = item.decoded_content.decode("utf-8", errors="replace")
        files.append({"path": item.path, "content": content})

    return files


def _report_progress(
    on_progress: ProgressCallback | None,
    stage: str,
    current: int,
    total: int,
    path: str,
) -> None:
    if on_progress is not None:
        on_progress(stage, current, total, path)
