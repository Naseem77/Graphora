"""FalkorDB graph store. Deterministic Cypher writes, no LLM, no embeddings.

Every relationship carries a `confidence` property:
- EXTRACTED: explicit in the source (import statement, definition, same-file call)
- INFERRED: resolved by name matching with exactly one candidate
- AMBIGUOUS: name matched multiple candidates; all are linked and tagged
"""

from __future__ import annotations

import hashlib
from typing import Any

from graphora.parser import AMBIGUOUS, EXTRACTED, INFERRED, ParsedFile


class GraphStore:
    """A named code graph for one project inside FalkorDB."""

    def __init__(self, project: str, host: str = "localhost", port: int = 6379, graph: Any | None = None):
        self.project = project
        if graph is not None:
            self._graph = graph
        else:
            try:
                from falkordb import FalkorDB
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("The 'falkordb' package is required: pip install falkordb") from exc
            self._graph = FalkorDB(host=host, port=port).select_graph(self.graph_name)

    @property
    def graph_name(self) -> str:
        return f"graphora:{self.project}"

    def query(self, cypher: str, params: dict | None = None) -> list[list]:
        result = self._graph.query(cypher, params or {})
        return getattr(result, "result_set", result) or []

    # --- writes ---------------------------------------------------------

    def write_files(self, parsed_files: list[ParsedFile]) -> int:
        for parsed in parsed_files:
            self.delete_file(parsed.path)
            self._write_file_node(parsed)
            self._write_imports(parsed)
            self._write_symbols(parsed)
            self._write_local_calls(parsed)
        return len(parsed_files)

    def link_cross_file_calls(self) -> int:
        """Second pass: resolve calls whose callee lives in another file.

        A single definition match is INFERRED; multiple matches are AMBIGUOUS.
        """
        rows = self.query(
            """
            MATCH (caller:Function)
            WHERE caller.pending_calls IS NOT NULL AND size(caller.pending_calls) > 0
            RETURN caller.stable_key, caller.pending_calls
            """
        )
        linked = 0
        for stable_key, pending in rows:
            for entry in pending or []:
                callee_name, line = entry.rsplit("@", 1)
                candidates = self.query(
                    "MATCH (f:Function {name: $name}) RETURN f.stable_key",
                    {"name": callee_name},
                )
                if not candidates:
                    continue
                confidence = INFERRED if len(candidates) == 1 else AMBIGUOUS
                for (callee_key,) in candidates:
                    if callee_key == stable_key:
                        continue
                    self.query(
                        """
                        MATCH (a:Function {stable_key: $a}), (b:Function {stable_key: $b})
                        MERGE (a)-[r:CALLS {line: $line}]->(b)
                        SET r.confidence = $confidence
                        """,
                        {"a": stable_key, "b": callee_key, "line": int(line), "confidence": confidence},
                    )
                    linked += 1
            self.query(
                "MATCH (f:Function {stable_key: $key}) SET f.pending_calls = NULL",
                {"key": stable_key},
            )
        return linked

    def delete_file(self, path: str) -> None:
        self.query(
            "MATCH (s)-[:DEFINED_IN]->(f:File {path: $path}) DETACH DELETE s",
            {"path": path},
        )
        self.query("MATCH (f:File {path: $path}) DETACH DELETE f", {"path": path})

    def clear(self) -> None:
        self.query("MATCH (n) DETACH DELETE n")

    def delete_graph(self) -> None:
        try:
            self._graph.delete()
        except Exception:
            pass

    # --- reads ------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        counts = {}
        for label, key in (("File", "files"), ("Function", "functions"), ("Class", "classes"), ("Module", "modules")):
            rows = self.query(f"MATCH (n:{label}) RETURN count(n)")
            counts[key] = int(rows[0][0]) if rows else 0
        rows = self.query("MATCH ()-[r:CALLS]->() RETURN count(r)")
        counts["calls"] = int(rows[0][0]) if rows else 0
        rows = self.query("MATCH ()-[r]->() RETURN count(r)")
        counts["edges"] = int(rows[0][0]) if rows else 0
        return counts

    def has_files(self) -> bool:
        rows = self.query("MATCH (f:File) RETURN count(f)")
        return bool(rows and int(rows[0][0]) > 0)

    # --- internals ----------------------------------------------------------

    def _write_file_node(self, parsed: ParsedFile) -> None:
        self.query(
            """
            MERGE (f:File {path: $path})
            SET f.language = $language,
                f.is_test = $is_test,
                f.content_hash = $content_hash,
                f.line_count = $line_count,
                f.parser = $parser
            """,
            {
                "path": parsed.path,
                "language": parsed.language,
                "is_test": parsed.is_test,
                "content_hash": _hash(parsed.source_text),
                "line_count": len(parsed.source_text.splitlines()),
                "parser": parsed.parser_used,
            },
        )

    def _write_imports(self, parsed: ParsedFile) -> None:
        for imp in parsed.imports:
            self.query(
                """
                MATCH (f:File {path: $path})
                MERGE (m:Module {name: $name})
                MERGE (f)-[r:IMPORTS]->(m)
                SET r.confidence = $confidence
                """,
                {"path": parsed.path, "name": imp.name, "confidence": imp.confidence},
            )

    def _write_symbols(self, parsed: ParsedFile) -> None:
        for symbol in parsed.symbols:
            label = "Function" if symbol.kind == "Function" else "Class"
            stable_key = f"{parsed.path}:{symbol.kind}:{symbol.name}"
            self.query(
                f"""
                MATCH (f:File {{path: $path}})
                MERGE (s:{label} {{stable_key: $stable_key}})
                SET s.name = $name,
                    s.path = $path,
                    s.line = $line,
                    s.signature = $signature,
                    s.signature_hash = $signature_hash,
                    s.confidence = $confidence,
                    s.is_test = $is_test
                MERGE (s)-[r:DEFINED_IN]->(f)
                SET r.confidence = $edge_confidence
                """,
                {
                    "path": parsed.path,
                    "stable_key": stable_key,
                    "name": symbol.name,
                    "line": symbol.line,
                    "signature": symbol.signature,
                    "signature_hash": _hash(symbol.signature),
                    "confidence": symbol.confidence,
                    "is_test": parsed.is_test,
                    "edge_confidence": EXTRACTED,
                },
            )

    def _write_local_calls(self, parsed: ParsedFile) -> None:
        local_functions = {s.name for s in parsed.symbols if s.kind == "Function"}
        pending: dict[str, list[str]] = {}
        for call in parsed.calls:
            if call.callee in local_functions:
                self.query(
                    """
                    MATCH (a:Function {stable_key: $a}), (b:Function {stable_key: $b})
                    MERGE (a)-[r:CALLS {line: $line}]->(b)
                    SET r.confidence = $confidence
                    """,
                    {
                        "a": f"{parsed.path}:Function:{call.caller}",
                        "b": f"{parsed.path}:Function:{call.callee}",
                        "line": call.line,
                        "confidence": call.confidence,
                    },
                )
            else:
                pending.setdefault(call.caller, []).append(f"{call.callee}@{call.line}")
        for caller, entries in pending.items():
            self.query(
                "MATCH (f:Function {stable_key: $key}) SET f.pending_calls = $entries",
                {"key": f"{parsed.path}:Function:{caller}", "entries": entries},
            )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
