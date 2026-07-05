from __future__ import annotations

import hashlib
from typing import Callable

from app.config import Settings, get_settings
from app.graph.sdk import graph_name
from app.parser.treesitter import ParsedFile


ProgressCallback = Callable[[str, int, int, str], None]

_CANDIDATE_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java")


def write_code_graph(
    owner: str,
    repo: str,
    parsed_files: list[ParsedFile],
    settings: Settings | None = None,
    suffix: str = "main",
    on_progress: ProgressCallback | None = None,
) -> int:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings, suffix)
    total = len(parsed_files)

    # Phase 1: write files, modules, and symbols for the whole batch first. CALLS
    # resolution (phase 2) needs every symbol in this batch to already exist,
    # otherwise a caller processed before its callee's file would never resolve.
    for index, parsed_file in enumerate(parsed_files, start=1):
        delete_file_subgraph(owner, repo, parsed_file.path, settings, suffix)
        file_hash = _hash_text(parsed_file.source_text)
        graph.query(
            """
            MERGE (file:File {id: $id})
            SET file.owner = $owner,
                file.repo = $repo,
                file.graph_scope = $graph_scope,
                file.path = $path,
                file.language = $language,
                file.content_hash = $content_hash,
                file.line_count = $line_count,
                file.symbol_count = $symbol_count
            """,
            {
                "id": _file_id(owner, repo, parsed_file.path),
                "owner": owner,
                "repo": repo,
                "graph_scope": suffix,
                "path": parsed_file.path,
                "language": parsed_file.language,
                "content_hash": file_hash,
                "line_count": len(parsed_file.source_text.splitlines()),
                "symbol_count": len(parsed_file.symbols),
            },
        )
        for import_name in parsed_file.imports:
            graph.query(
                """
                MATCH (file:File {path: $path})
                MERGE (module:Module {id: $id})
                SET module.name = $name,
                    module.graph_scope = $graph_scope
                MERGE (file)-[:IMPORTS]->(module)
                """,
                {
                    "path": parsed_file.path,
                    "id": _module_id(owner, repo, import_name),
                    "name": import_name,
                    "graph_scope": suffix,
                },
            )
            graph.query(
                """
                MATCH (file:File {path: $path})-[:IMPORTS]->(module:Module {id: $id})
                MERGE (file)-[:DEPENDS_ON]->(module)
                """,
                {"path": parsed_file.path, "id": _module_id(owner, repo, import_name)},
            )
        for symbol in parsed_file.symbols:
            label = _symbol_label(symbol.kind)
            stable_id = _symbol_id(owner, repo, parsed_file.path, symbol.kind, symbol.name)
            signature_hash = _hash_text(symbol.signature)
            graph.query(
                f"""
                MATCH (file:File {{path: $path}})
                MERGE (symbol:{label} {{id: $id}})
                SET symbol.owner = $owner,
                    symbol.repo = $repo,
                    symbol.graph_scope = $graph_scope,
                    symbol.kind = $kind,
                    symbol.name = $name,
                    symbol.path = $path,
                    symbol.line = $line,
                    symbol.signature = $signature,
                    symbol.signature_hash = $signature_hash,
                    symbol.stable_key = $stable_key
                MERGE (symbol)-[:DEFINED_IN]->(file)
                """,
                {
                    "id": stable_id,
                    "owner": owner,
                    "repo": repo,
                    "graph_scope": suffix,
                    "kind": symbol.kind,
                    "name": symbol.name,
                    "path": parsed_file.path,
                    "line": symbol.line,
                    "signature": symbol.signature,
                    "signature_hash": signature_hash,
                    "stable_key": f"{parsed_file.path}:{symbol.kind}:{symbol.name}",
                },
            )
        if parsed_file.language == "markdown":
            doc_id = _doc_id(owner, repo, parsed_file.path)
            graph.query(
                """
                MATCH (file:File {path: $path})
                MERGE (doc:DocPage {id: $id})
                SET doc.owner = $owner,
                    doc.repo = $repo,
                    doc.graph_scope = $graph_scope,
                    doc.path = $path,
                    doc.title = $title,
                    doc.content_hash = $content_hash
                MERGE (doc)-[:DOCUMENTS]->(file)
                """,
                {
                    "id": doc_id,
                    "owner": owner,
                    "repo": repo,
                    "graph_scope": suffix,
                    "path": parsed_file.path,
                    "title": parsed_file.doc_sections[0].title if parsed_file.doc_sections else parsed_file.path,
                    "content_hash": file_hash,
                },
            )
            for section in parsed_file.doc_sections:
                graph.query(
                    """
                    MATCH (doc:DocPage {id: $doc_id})
                    MERGE (section:DocSection {id: $id})
                    SET section.title = $title,
                        section.level = $level,
                        section.line = $line,
                        section.stable_key = $stable_key,
                        section.graph_scope = $graph_scope
                    MERGE (doc)-[:HAS_SECTION]->(section)
                    """,
                    {
                        "doc_id": doc_id,
                        "id": _doc_section_id(owner, repo, section.stable_key),
                        "title": section.title,
                        "level": section.level,
                        "line": section.line,
                        "stable_key": section.stable_key,
                        "graph_scope": suffix,
                    },
                )
        if on_progress is not None:
            on_progress("writing structural graph", index, total, parsed_file.path)

    # Phase 2: resolve CALLS edges now that every symbol in the batch (plus
    # anything already persisted from earlier builds) exists. A callee is
    # resolved either in the caller's own file, or in a file that the caller's
    # file imports (import name -> candidate file path). This intentionally
    # does NOT fall back to matching a function name anywhere in the repo,
    # since that would create false-positive cross-file edges between
    # unrelated same-named functions.
    for parsed_file in parsed_files:
        import_paths = _import_candidate_paths(parsed_file.imports)
        for import_name in parsed_file.imports:
            for target_path in _module_candidate_paths(import_name):
                graph.query(
                    """
                    MATCH (module:Module {id: $module_id})
                    MATCH (target:File {path: $target_path})
                    MERGE (module)-[:RESOLVES_TO]->(target)
                    """,
                    {
                        "module_id": _module_id(owner, repo, import_name),
                        "target_path": target_path,
                    },
                )
        for call in parsed_file.calls:
            graph.query(
                """
                MATCH (caller:Function {stable_key: $caller_key})
                MATCH (callee:Function {name: $callee})
                WHERE callee.path = $path OR callee.path IN $import_paths
                MERGE (caller)-[r:CALLS]->(callee)
                SET r.line = $line,
                    r.resolution = CASE WHEN callee.path = $path THEN 'same_file' ELSE 'import' END
                MERGE (caller)-[:DEPENDS_ON]->(callee)
                """,
                {
                    "caller_key": f"{parsed_file.path}:Function:{call.caller}",
                    "path": parsed_file.path,
                    "callee": call.callee,
                    "line": call.line,
                    "import_paths": import_paths,
                },
            )
    return len(parsed_files)


def clear_code_graph(
    owner: str,
    repo: str,
    settings: Settings | None = None,
    suffix: str = "main",
) -> None:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings, suffix)
    for label in ("Function", "Class", "File", "Module", "DocPage", "DocSection"):
        graph.query(f"MATCH (n:{label}) DETACH DELETE n")


def delete_code_graph(
    owner: str,
    repo: str,
    settings: Settings | None = None,
    suffix: str = "main",
) -> None:
    settings = settings or get_settings()
    _select_graph(owner, repo, settings, suffix).delete()


def code_graph_has_files(
    owner: str,
    repo: str,
    settings: Settings | None = None,
    suffix: str = "main",
) -> bool:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings, suffix)
    result = graph.query("MATCH (file:File) RETURN count(file) LIMIT 1")
    rows = getattr(result, "result_set", result) or []
    if not rows:
        return False
    return int(rows[0][0] or 0) > 0


def symbol_impact(
    owner: str,
    repo: str,
    symbols: list[str],
    settings: Settings | None = None,
    suffix: str = "main",
    symbol_limit: int = 25,
    row_limit: int = 15,
) -> list[dict]:
    """Return structural impact for changed symbols, queried from the code graph.

    For each symbol name this reports where it is defined, which functions call
    it (callers), which functions it calls (callees), and any tests that exercise
    it. This replaces GraphRAG retrieval with deterministic Cypher traversals.
    """
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings, suffix)

    def rows(query: str, params: dict) -> list[list]:
        result = graph.query(query, params)
        return getattr(result, "result_set", result) or []

    impacts: list[dict] = []
    for name in list(dict.fromkeys(symbols))[:symbol_limit]:
        params = {"name": name, "limit": row_limit}
        definitions = [
            {"kind": r[0], "path": r[1], "line": r[2], "signature": r[3]}
            for r in rows(
                """
                MATCH (symbol)-[:DEFINED_IN]->(file:File)
                WHERE (symbol:Function OR symbol:Class) AND symbol.name = $name
                RETURN symbol.kind, symbol.path, symbol.line, symbol.signature
                LIMIT $limit
                """,
                params,
            )
        ]
        callers = [
            {"name": r[0], "path": r[1]}
            for r in rows(
                """
                MATCH (caller:Function)-[:CALLS]->(callee:Function {name: $name})
                RETURN DISTINCT caller.name, caller.path
                LIMIT $limit
                """,
                params,
            )
        ]
        callees = [
            {"name": r[0], "path": r[1]}
            for r in rows(
                """
                MATCH (symbol:Function {name: $name})-[:CALLS]->(callee:Function)
                RETURN DISTINCT callee.name, callee.path
                LIMIT $limit
                """,
                params,
            )
        ]
        tests = [
            {"name": r[0], "path": r[1]}
            for r in rows(
                """
                MATCH (test:Function)-[:CALLS]->(callee:Function {name: $name})
                WHERE test.name STARTS WITH 'test' OR test.path CONTAINS 'test'
                RETURN DISTINCT test.name, test.path
                LIMIT $limit
                """,
                params,
            )
        ]
        if definitions or callers or callees or tests:
            impacts.append(
                {
                    "name": name,
                    "definitions": definitions,
                    "callers": callers,
                    "callees": callees,
                    "tests": tests,
                }
            )
    return impacts


def code_graph_stats(
    owner: str,
    repo: str,
    settings: Settings | None = None,
    suffix: str = "main",
) -> dict:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings, suffix)

    label_counts: dict[str, int] = {}
    node_result = graph.query("MATCH (n) RETURN labels(n), count(n)")
    for row in getattr(node_result, "result_set", node_result) or []:
        labels = row[0] or []
        label = labels[0] if isinstance(labels, list) and labels else "Unlabeled"
        label_counts[label] = label_counts.get(label, 0) + int(row[1] or 0)

    rel_result = graph.query("MATCH ()-[r]->() RETURN count(r)")
    rel_rows = getattr(rel_result, "result_set", rel_result) or []
    relationship_count = int(rel_rows[0][0] or 0) if rel_rows else 0

    file_result = graph.query("MATCH (file:File) RETURN file.path ORDER BY file.path")
    files = [row[0] for row in (getattr(file_result, "result_set", file_result) or []) if row[0]]

    return {
        "node_counts": label_counts,
        "node_total": sum(label_counts.values()),
        "relationship_count": relationship_count,
        "files": files,
    }


def delete_file_subgraph(
    owner: str,
    repo: str,
    path: str,
    settings: Settings | None = None,
    suffix: str = "main",
) -> None:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings, suffix)
    graph.query(
        """
        MATCH (symbol)-[:DEFINED_IN]->(file:File {path: $path})
        DETACH DELETE symbol
        """,
        {"path": path},
    )
    graph.query(
        """
        MATCH (doc:DocPage {path: $path})-[:HAS_SECTION]->(section:DocSection)
        DETACH DELETE section
        """,
        {"path": path},
    )
    graph.query("MATCH (file:File {path: $path}) DETACH DELETE file", {"path": path})
    graph.query("MATCH (doc:DocPage {path: $path}) DETACH DELETE doc", {"path": path})


def _select_graph(owner: str, repo: str, settings: Settings, suffix: str = "main"):
    try:
        from falkordb import FalkorDB
    except ImportError as exc:
        raise RuntimeError("falkordb is required for graph storage") from exc

    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    return db.select_graph(graph_name(owner, repo, suffix))


def _symbol_label(kind: str) -> str:
    if kind not in {"Function", "Class"}:
        raise ValueError(f"Unsupported symbol kind: {kind}")
    return kind


def _file_id(owner: str, repo: str, path: str) -> str:
    return f"{owner}/{repo}:file:{path}"


def _module_id(owner: str, repo: str, name: str) -> str:
    return f"{owner}/{repo}:module:{name}"


def _symbol_id(owner: str, repo: str, path: str, kind: str, name: str) -> str:
    return f"{owner}/{repo}:symbol:{path}:{kind}:{name}"


def _doc_id(owner: str, repo: str, path: str) -> str:
    return f"{owner}/{repo}:doc:{path}"


def _doc_section_id(owner: str, repo: str, stable_key: str) -> str:
    return f"{owner}/{repo}:doc-section:{stable_key}"


def _module_candidate_paths(import_name: str) -> list[str]:
    """Best-effort mapping from an import string to candidate internal file paths.

    Handles Python dotted imports (``app.graph.store`` -> ``app/graph/store.py``)
    and path-style imports (``./utils`` -> ``utils.ts``/``utils.js``/...). This is
    intentionally conservative: candidates are only used to MATCH an existing
    ``File`` node, so an import that doesn't resolve to a real file in the repo
    simply yields no match instead of a false positive.
    """
    normalized = import_name.strip().lstrip("./")
    if not normalized:
        return []
    base = normalized.replace(".", "/") if "." in normalized and "/" not in normalized else normalized

    candidates: set[str] = set()
    for ext in _CANDIDATE_EXTENSIONS:
        candidates.add(f"{base}{ext}")
        candidates.add(f"{base}/__init__{ext}")
        candidates.add(f"{base}/index{ext}")
    return sorted(candidates)


def _import_candidate_paths(import_names: list[str]) -> list[str]:
    paths: set[str] = set()
    for import_name in import_names:
        paths.update(_module_candidate_paths(import_name))
    return sorted(paths)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
