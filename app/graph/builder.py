from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github import Github

from app.graph.sdk import create_source, get_kg
from app.parser.treesitter import is_supported_code_file, parse_code_file


logger = logging.getLogger(__name__)


RepoFile = dict[str, str]


def build_repo_graph(owner: str, repo: str, files: list[RepoFile]) -> int:
    kg = get_kg(owner, repo)
    sources = [
        create_source(parse_code_file(file["path"], file["content"]).source_text)
        for file in files
        if is_supported_code_file(file["path"])
    ]
    if not sources:
        logger.info("No supported code files found for %s/%s", owner, repo)
        return 0

    kg.process_sources(sources)
    logger.info("Built graph for %s/%s with %s source files", owner, repo, len(sources))
    return len(sources)


def fetch_repository_files(github_client: "Github", owner: str, repo: str, ref: str | None = None) -> list[RepoFile]:
    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    queue = list(repo_obj.get_contents("", ref=ref))
    files: list[RepoFile] = []

    while queue:
        item = queue.pop(0)
        if item.type == "dir":
            queue.extend(repo_obj.get_contents(item.path, ref=ref))
            continue
        if not is_supported_code_file(item.path):
            continue
        content = item.decoded_content.decode("utf-8", errors="replace")
        files.append({"path": item.path, "content": content})

    return files
