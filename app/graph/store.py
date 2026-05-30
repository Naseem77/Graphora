from __future__ import annotations

from app.config import Settings, get_settings
from app.graph.sdk import graph_name
from app.parser.treesitter import ParsedFile


def write_code_graph(
    owner: str,
    repo: str,
    parsed_files: list[ParsedFile],
    settings: Settings | None = None,
) -> int:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings)
    for parsed_file in parsed_files:
        delete_file_subgraph(owner, repo, parsed_file.path, settings)
        graph.query(
            "MERGE (file:File {path: $path}) SET file.language = $language",
            {"path": parsed_file.path, "language": parsed_file.language},
        )
        for import_name in parsed_file.imports:
            graph.query(
                """
                MATCH (file:File {path: $path})
                MERGE (module:Module {name: $name})
                MERGE (file)-[:IMPORTS]->(module)
                """,
                {"path": parsed_file.path, "name": import_name},
            )
        for symbol in parsed_file.symbols:
            label = _symbol_label(symbol.kind)
            graph.query(
                f"""
                MATCH (file:File {{path: $path}})
                MERGE (symbol:{label} {{id: $id}})
                SET symbol.name = $name,
                    symbol.path = $path,
                    symbol.line = $line,
                    symbol.signature = $signature
                MERGE (symbol)-[:DEFINED_IN]->(file)
                """,
                {
                    "id": f"{parsed_file.path}:{symbol.kind}:{symbol.name}:{symbol.line}",
                    "name": symbol.name,
                    "path": parsed_file.path,
                    "line": symbol.line,
                    "signature": symbol.signature,
                },
            )
    return len(parsed_files)


def clear_code_graph(owner: str, repo: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings)
    for label in ("Function", "Class", "File", "Module"):
        graph.query(f"MATCH (n:{label}) DETACH DELETE n")


def delete_file_subgraph(owner: str, repo: str, path: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    graph = _select_graph(owner, repo, settings)
    graph.query(
        """
        MATCH (symbol)-[:DEFINED_IN]->(file:File {path: $path})
        DETACH DELETE symbol
        """,
        {"path": path},
    )
    graph.query("MATCH (file:File {path: $path}) DETACH DELETE file", {"path": path})


def _select_graph(owner: str, repo: str, settings: Settings):
    try:
        from falkordb import FalkorDB
    except ImportError as exc:
        raise RuntimeError("falkordb is required for graph storage") from exc

    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    return db.select_graph(graph_name(owner, repo))


def _symbol_label(kind: str) -> str:
    if kind not in {"Function", "Class"}:
        raise ValueError(f"Unsupported symbol kind: {kind}")
    return kind
