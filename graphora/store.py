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

    backend = "falkordb"

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

    def save(self) -> None:
        """No-op for FalkorDB (writes are already persistent)."""

    # --- named reads (shared surface with EmbeddedGraphStore) ---------------

    def find_definitions(self, name: str) -> list[list]:
        return self.query(
            """
            MATCH (s)
            WHERE (s:Function OR s:Class) AND s.name = $name
            RETURN s.name, labels(s)[0], s.path, s.line, s.signature,
                   coalesce(s.risk_score, 0.0), coalesce(s.fix_count, 0),
                   coalesce(s.last_broke_at, '')
            """,
            {"name": name},
        )

    def callers_of(self, name: str, path: str) -> list[tuple[str, str, str]]:
        return [
            (r[0], r[1], r[2])
            for r in self.query(
                """
                MATCH (caller:Function)-[r:CALLS]->(s:Function {name: $name, path: $path})
                WHERE coalesce(caller.is_test, false) = false
                RETURN DISTINCT caller.name, caller.path, r.confidence
                """,
                {"name": name, "path": path},
            )
        ]

    def callees_of(self, name: str, path: str) -> list[tuple[str, str, str]]:
        return [
            (r[0], r[1], r[2])
            for r in self.query(
                """
                MATCH (s:Function {name: $name, path: $path})-[r:CALLS]->(callee:Function)
                RETURN DISTINCT callee.name, callee.path, r.confidence
                """,
                {"name": name, "path": path},
            )
        ]

    def tests_covering(self, name: str, path: str) -> list[tuple[str, str, str]]:
        return [
            (r[0], r[1], r[2])
            for r in self.query(
                """
                MATCH (t:Function {is_test: true})-[r:CALLS]->(s:Function {name: $name, path: $path})
                RETURN DISTINCT t.name, t.path, r.confidence
                """,
                {"name": name, "path": path},
            )
        ]

    def importers_of_module(self, module_hint: str, exclude_path: str) -> list[str]:
        return [
            r[0]
            for r in self.query(
                """
                MATCH (f:File)-[:IMPORTS]->(m:Module)
                WHERE m.name ENDS WITH $module_hint AND f.path <> $path
                RETURN DISTINCT f.path
                """,
                {"module_hint": module_hint, "path": exclude_path},
            )
        ]

    def top_connected_symbols(self, count: int = 3) -> list[str]:
        rows = self.query(
            """
            MATCH (caller:Function)-[:CALLS]->(s:Function)
            WHERE coalesce(s.is_test, false) = false
            WITH s.name AS name, count(caller) AS degree
            WITH name, sum(degree) AS total_degree
            ORDER BY total_degree DESC, name ASC
            RETURN name LIMIT $count
            """,
            {"count": count},
        )
        return [r[0] for r in rows]

    def find_symbol(self, name: str) -> list[dict]:
        rows = self.query(
            """
            MATCH (s) WHERE (s:Function OR s:Class) AND s.name = $name
            RETURN s.name, labels(s)[0], s.path, s.line, s.signature
            """,
            {"name": name},
        )
        return [
            {"name": r[0], "kind": r[1], "path": r[2], "line": int(r[3] or 0), "signature": r[4]}
            for r in rows
        ]

    # --- risk memory surface -------------------------------------------------

    def known_fix_shas(self) -> set[str]:
        return {row[0] for row in self.query("MATCH (c:FixCommit) RETURN c.sha")}

    def upsert_fix_commit(self, sha: str, date: str, subject: str, kind: str) -> None:
        self.query(
            "MERGE (c:FixCommit {sha: $sha}) SET c.date = $date, c.subject = $subject, c.kind = $kind",
            {"sha": sha, "date": date, "subject": subject[:300], "kind": kind},
        )

    def touch_file(self, sha: str, path: str) -> int:
        rows = self.query(
            """
            MATCH (f:File {path: $path}), (c:FixCommit {sha: $sha})
            MERGE (c)-[:TOUCHED]->(f)
            RETURN f.path
            """,
            {"path": path, "sha": sha},
        )
        return len(rows)

    def record_symbol_fix(self, path: str, name: str, sha: str, date: str) -> int:
        rows = self.query(
            """
            MATCH (s {path: $path, name: $name}), (c:FixCommit {sha: $sha})
            WHERE s:Function OR s:Class
            MERGE (c)-[:FIXED]->(s)
            SET s.fix_count = coalesce(s.fix_count, 0) + 1,
                s.last_broke_at =
                    CASE WHEN coalesce(s.last_broke_at, '') < $date THEN $date
                         ELSE s.last_broke_at END
            RETURN s.name
            """,
            {"path": path, "name": name, "sha": sha, "date": date},
        )
        return len(rows)

    def risky_symbols(self, limit: int = 15) -> list[list]:
        return self.query(
            """
            MATCH (s)
            WHERE (s:Function OR s:Class) AND coalesce(s.fix_count, 0) > 0
            OPTIONAL MATCH (caller:Function)-[:CALLS]->(s)
            WITH s, count(DISTINCT caller) AS caller_count
            RETURN s.name, s.path, s.line, coalesce(s.fix_count, 0),
                   coalesce(s.last_broke_at, ''), coalesce(s.risk_score, 0.0), caller_count
            ORDER BY coalesce(s.risk_score, 0.0) DESC, coalesce(s.fix_count, 0) DESC
            LIMIT $limit
            """,
            {"limit": limit},
        )

    def symbols_with_fixes(self) -> list[list]:
        return self.query(
            """
            MATCH (s)
            WHERE (s:Function OR s:Class) AND coalesce(s.fix_count, 0) > 0
            RETURN s.path, s.name, s.fix_count, coalesce(s.last_broke_at, '')
            """
        )

    def set_risk_score(self, path: str, name: str, score: float) -> None:
        self.query(
            """
            MATCH (s {path: $path, name: $name})
            WHERE s:Function OR s:Class
            SET s.risk_score = $score
            """,
            {"path": path, "name": name, "score": score},
        )

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


def falkordb_available(host: str = "localhost", port: int = 6379, timeout: float = 0.5) -> bool:
    """Fast reachability probe (TCP connect), no client dependency."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def open_store(
    project: str,
    backend: str = "auto",
    host: str = "localhost",
    port: int = 6379,
    data_dir: str | None = None,
):
    """Open a graph store.

    backend:
    - "falkordb": live FalkorDB graph (server required)
    - "embedded": pure-Python JSON store, zero Docker
    - "auto": FalkorDB if reachable, otherwise embedded
    """
    if backend == "falkordb":
        return GraphStore(project, host=host, port=port)
    if backend == "embedded":
        from graphora.embedded import EmbeddedGraphStore

        return EmbeddedGraphStore(project, data_dir=data_dir)
    if backend == "auto":
        if falkordb_available(host, port):
            return GraphStore(project, host=host, port=port)
        import sys

        from graphora.embedded import EmbeddedGraphStore

        print(
            f"graphora: FalkorDB not reachable at {host}:{port}, using embedded backend",
            file=sys.stderr,
        )
        return EmbeddedGraphStore(project, data_dir=data_dir)
    raise ValueError(f"Unknown backend: {backend!r} (use auto, falkordb, or embedded)")
