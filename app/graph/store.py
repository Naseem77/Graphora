from __future__ import annotations

import hashlib

from app.config import Settings, get_settings
from app.graph.sdk import graph_name
from app.parser.treesitter import ParsedFile


def write_code_graph(
    owner: str,
    repo: str,
    parsed_files: list[ParsedFile],
    settings: Settings | None = None,
    suffix: str = "main",
) -> int:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings, suffix)
    for parsed_file in parsed_files:
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
        for call in parsed_file.calls:
            graph.query(
                """
                MATCH (caller:Function {stable_key: $caller_key})
                MATCH (callee:Function {path: $path, name: $callee})
                MERGE (caller)-[:CALLS {line: $line}]->(callee)
                MERGE (caller)-[:DEPENDS_ON]->(callee)
                """,
                {
                    "caller_key": f"{parsed_file.path}:Function:{call.caller}",
                    "path": parsed_file.path,
                    "callee": call.callee,
                    "line": call.line,
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


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
