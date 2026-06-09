from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from app.config import Settings, get_settings
from app.graph.pr_graph import pr_suffix
from app.graph.store import _select_graph


@dataclass(frozen=True)
class GraphEvidence:
    changed_files: list[str]
    failed_tests: list[str]
    failed_paths: list[str]
    dependency_paths: list[str]
    related_symbols: list[str]

    def to_prompt_context(self) -> str:
        return "\n".join(
            [
                "Graph evidence:",
                f"- Changed files: {', '.join(self.changed_files) or 'unknown'}",
                f"- Failed tests: {', '.join(self.failed_tests) or 'unknown'}",
                f"- Failed paths from logs: {', '.join(self.failed_paths) or 'unknown'}",
                "- Dependency paths:",
                *[f"  - {path}" for path in self.dependency_paths[:10]],
                "- Related changed symbols:",
                *[f"  - {symbol}" for symbol in self.related_symbols[:20]],
            ]
        )


def build_ci_graph_evidence(
    owner: str,
    repo: str,
    pr_number: int,
    changed_files: list[str],
    failed_tests: list[str],
    log_paths: list[str],
    settings: Settings | None = None,
) -> GraphEvidence:
    settings = settings or get_settings()
    failed_paths = _candidate_failed_paths(failed_tests, log_paths)
    graph = _select_graph(owner, repo, settings, pr_suffix(pr_number))
    dependency_paths = _dependency_paths(graph, changed_files, failed_paths)
    related_symbols = _changed_symbols(graph, changed_files)
    return GraphEvidence(
        changed_files=changed_files,
        failed_tests=failed_tests,
        failed_paths=failed_paths,
        dependency_paths=dependency_paths,
        related_symbols=related_symbols,
    )


def _candidate_failed_paths(failed_tests: list[str], log_paths: list[str]) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for test in failed_tests:
        path = test.split("::", 1)[0]
        if not path.endswith((".py", ".go", ".js", ".ts", ".tsx", ".jsx", ".java")):
            path = path.replace(".", "/")
        if "/" not in path and not path.endswith((".py", ".go", ".js", ".ts", ".tsx", ".jsx", ".java")):
            continue
        if "." not in PurePosixPath(path).name:
            path = f"{path}.py"
        if path not in seen:
            seen.add(path)
            paths.append(path)
    for path in log_paths:
        if _looks_like_test_path(path) and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths[:25]


def _dependency_paths(graph: object, changed_files: list[str], failed_paths: list[str]) -> list[str]:
    if not changed_files or not failed_paths:
        return []
    query = """
    MATCH (changed:File)
    WHERE changed.path IN $changed_files
    MATCH (failed:File)
    WHERE failed.path IN $failed_paths
    MATCH path = shortestPath((failed)-[:DEPENDS_ON|IMPORTS|CALLS*..6]-(changed))
    RETURN failed.path, changed.path, [node IN nodes(path) | coalesce(node.path, node.stable_key, node.name)] AS path_nodes
    LIMIT 10
    """
    rows = _result_rows(graph.query(query, {"changed_files": changed_files, "failed_paths": failed_paths}))
    paths: list[str] = []
    for row in rows:
        if len(row) >= 3 and isinstance(row[2], list):
            paths.append(" -> ".join(str(item) for item in row[2] if item))
        elif len(row) >= 2:
            paths.append(f"{row[0]} -> {row[1]}")
    return paths


def _changed_symbols(graph: object, changed_files: list[str]) -> list[str]:
    if not changed_files:
        return []
    query = """
    MATCH (symbol)-[:DEFINED_IN]->(file:File)
    WHERE file.path IN $changed_files AND (symbol:Function OR symbol:Class)
    RETURN symbol.stable_key
    LIMIT 50
    """
    rows = _result_rows(graph.query(query, {"changed_files": changed_files}))
    return [str(row[0]) for row in rows if row and row[0]]


def _result_rows(result: object) -> list:
    return list(getattr(result, "result_set", result) or [])


def _looks_like_test_path(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return path.startswith(("test/", "tests/")) or name.startswith("test_") or ".test." in name or ".spec." in name
