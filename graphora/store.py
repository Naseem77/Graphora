"""FalkorDB graph store. Deterministic Cypher writes, no LLM, no embeddings.

Every CALLS relationship carries `confidence` (see graphora.resolve):
- EXTRACTED: the source names the target (same file, self, import)
- INFERRED: a bare call whose name matches exactly one definition
- AMBIGUOUS: several candidates (all linked) or an unknown receiver type
plus `candidates`, the number of targets its call site resolved to.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Union

from graphora.parser import EXTRACTED, ParsedFile
from graphora.resolve import CallSite, FileRecord, SymbolRecord, records_for, resolve

_BATCH = 2000
_SYMBOL_FIELDS = (
    "name", "qualname", "kind", "path", "line", "end_line", "signature", "parent", "parent_kind", "is_test", "confidence",
    "returns", "attributes",
)
_INDEXES = (
    ("File", "path"),
    ("Function", "stable_key"),
    ("Function", "name"),
    ("Function", "qualname"),
    ("Function", "path"),
    ("Class", "stable_key"),
    ("Class", "name"),
    ("Class", "qualname"),
    ("Class", "path"),
    ("Module", "name"),
)


class GraphStore:
    """A named code graph for one project inside FalkorDB."""

    backend = "falkordb"

    def __init__(self, project: str, host: str = "localhost", port: int = 6379, graph: Any | None = None):
        self.project = project
        self.host, self.port = host, port
        self._db: Any | None = None
        if graph is not None:
            self._graph = graph
        else:
            try:
                from falkordb import FalkorDB
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("The 'falkordb' package is required: pip install falkordb") from exc
            self._db = FalkorDB(host=host, port=port)
            self._graph = self._db.select_graph(self.graph_name)
        self._indexed = False

    @property
    def graph_name(self) -> str:
        return f"graphora:{self.project}"

    def query(self, cypher: str, params: dict | None = None) -> list[list]:
        result = self._graph.query(cypher, params or {})
        return getattr(result, "result_set", result) or []

    def _ensure_indexes(self) -> None:
        if self._indexed:
            return
        for label, prop in _INDEXES:
            try:
                self.query(f"CREATE INDEX FOR (n:{label}) ON (n.{prop})")
            except Exception:  # noqa: BLE001 - already exists
                pass
        self._indexed = True

    # --- writes ---------------------------------------------------------

    def write_files(self, parsed_files: list[ParsedFile], save: bool = True) -> int:
        """Store files, their symbols, and raw call sites (batched per file).

        `save` is accepted for parity with the embedded store; FalkorDB
        writes are persistent immediately. Call link_cross_file_calls()
        afterwards to (re)build the CALLS edges.
        """
        self._ensure_indexes()
        for parsed in parsed_files:
            self.delete_file(parsed.path)
            symbols, record = records_for(parsed)
            self.query(
                """
                CREATE (f:File {path: $path})
                SET f.language = $language, f.is_test = $is_test, f.content_hash = $content_hash,
                    f.line_count = $line_count, f.parser = $parser,
                    f.bindings = $bindings, f.raw_calls = $raw_calls
                """,
                {
                    "path": parsed.path,
                    "language": parsed.language,
                    "is_test": parsed.is_test,
                    "content_hash": _hash(parsed.source_text),
                    "line_count": parsed.source_text.count("\n") + (0 if parsed.source_text.endswith("\n") else 1),
                    "parser": parsed.parser_used,
                    "bindings": json.dumps([list(b) for b in record.bindings]),
                    "raw_calls": json.dumps([[c.caller_key, c.callee, c.receiver, c.line] for c in record.calls]),
                },
            )
            for label in ("Function", "Class"):
                rows = [_symbol_row(s) for s in symbols if s.kind == label]
                for start in range(0, len(rows), _BATCH):
                    self.query(
                        f"""
                        MATCH (f:File {{path: $path}})
                        UNWIND $rows AS row
                        CREATE (s:{label} {{stable_key: row.key}})
                        SET s += row.props, s.signature_hash = row.signature_hash
                        CREATE (s)-[:DEFINED_IN {{confidence: '{EXTRACTED}'}}]->(f)
                        """,
                        {"path": parsed.path, "rows": rows[start : start + _BATCH]},
                    )
            if parsed.imports:
                self.query(
                    """
                    MATCH (f:File {path: $path})
                    UNWIND $rows AS row
                    MERGE (m:Module {name: row.name})
                    MERGE (f)-[r:IMPORTS]->(m)
                    SET r.confidence = row.confidence
                    """,
                    {"path": parsed.path, "rows": [{"name": i.name, "confidence": i.confidence} for i in parsed.imports]},
                )
        return len(parsed_files)

    def link_cross_file_calls(self) -> int:
        """Resolve every stored call site into CALLS edges (all files, so
        callers in unchanged files relink to callees in changed ones)."""
        self._ensure_indexes()
        symbols = [
            SymbolRecord(key=row[0], **dict(zip(_SYMBOL_FIELDS, row[1:])))
            for row in self.query(
                "MATCH (s) WHERE s:Function OR s:Class RETURN s.stable_key, "
                + ", ".join(f"s.{f}" for f in _SYMBOL_FIELDS)
            )
        ]
        files = [
            FileRecord(
                path=path,
                language=language,
                is_test=bool(is_test),
                bindings=tuple((*b, "")[:4] for b in json.loads(bindings or "[]")),
                calls=tuple(CallSite(*c) for c in json.loads(raw_calls or "[]")),
            )
            for path, language, is_test, bindings, raw_calls in self.query(
                "MATCH (f:File) RETURN f.path, f.language, f.is_test, f.bindings, f.raw_calls"
            )
        ]
        result = resolve(symbols, files)
        self.query("MATCH ()-[r:CALLS]->() DELETE r")
        self.query("MATCH ()-[r:IMPORTS_FILE]->() DELETE r")
        kinds = {s.key: s.kind for s in symbols}
        for label in ("Function", "Class"):
            rows = [dict(edge.__dict__) for edge in result.edges if kinds.get(edge.b) == label]
            for start in range(0, len(rows), _BATCH):
                self.query(
                    f"""
                    UNWIND $rows AS row
                    MATCH (a:Function {{stable_key: row.a}}), (b:{label} {{stable_key: row.b}})
                    CREATE (a)-[:CALLS {{line: row.line, confidence: row.confidence,
                                         candidates: row.candidates, reason: row.reason}}]->(b)
                    """,
                    {"rows": rows[start : start + _BATCH]},
                )
        pairs = [{"a": a, "b": b} for a, b in result.file_imports]
        for start in range(0, len(pairs), _BATCH):
            self.query(
                """
                UNWIND $rows AS row
                MATCH (a:File {path: row.a}), (b:File {path: row.b})
                CREATE (a)-[:IMPORTS_FILE]->(b)
                """,
                {"rows": pairs[start : start + _BATCH]},
            )
        return len(result.edges)

    def delete_file(self, path: str) -> None:
        self.query("MATCH (s)-[:DEFINED_IN]->(f:File {path: $path}) DETACH DELETE s", {"path": path})
        self.query("MATCH (f:File {path: $path}) DETACH DELETE f", {"path": path})

    def clear(self) -> None:
        self.query("MATCH (n) DETACH DELETE n")

    def delete_graph(self) -> None:
        try:
            self._graph.delete()
        except Exception:
            pass

    # --- agent sessions (see graphora/sessions.py) -------------------------

    def upsert_session(self, sid: str, props: dict) -> None:
        self.query(
            """MERGE (s:Session {id: $id})
               SET s.agent = $agent, s.summary = $summary, s.cwd = $cwd, s.branch = $branch,
                   s.created_at = $created_at, s.updated_at = $updated_at, s.last_ask = $last_ask""",
            {"id": sid, "agent": "", **props},
        )

    def link_session_repo(self, sid: str, repo: str) -> None:
        self.query(
            """MERGE (r:Repo {name: $repo})
               WITH r MATCH (s:Session {id: $id}) MERGE (s)-[:IN_REPO]->(r)""",
            {"repo": repo, "id": sid},
        )

    def link_session_file(self, sid: str, path: str, tool: str) -> None:
        self.query(
            """MERGE (f:WorkFile {path: $path})
               WITH f MATCH (s:Session {id: $id})
               MERGE (s)-[t:TOUCHED]->(f) SET t.tool = $tool""",
            {"path": path, "id": sid, "tool": tool},
        )

    def link_session_ref(self, sid: str, kind: str, value: str) -> None:
        self.query(
            """MERGE (x:Ref {kind: $kind, value: $value})
               WITH x MATCH (s:Session {id: $id}) MERGE (s)-[:REFERENCES]->(x)""",
            {"kind": kind, "value": value, "id": sid},
        )

    def sessions_connected(self, kind: str, value: str) -> list[list]:
        if kind == "file":
            cypher = """MATCH (s:Session)-[:TOUCHED]->(f:WorkFile)
                        WHERE f.path ENDS WITH $v
                        RETURN s.id, s.summary, s.updated_at, f.path, s.agent
                        ORDER BY s.updated_at DESC"""
        elif kind == "repo":
            cypher = """MATCH (s:Session)-[:IN_REPO]->(r:Repo {name: $v})
                        RETURN s.id, s.summary, s.updated_at, r.name, s.agent
                        ORDER BY s.updated_at DESC"""
        else:
            cypher = """MATCH (s:Session)-[:REFERENCES]->(x:Ref)
                        WHERE x.value = $v OR x.value ENDS WITH $v
                        RETURN s.id, s.summary, s.updated_at, x.kind + ' ' + x.value, s.agent
                        ORDER BY s.updated_at DESC"""
        return self.query(cypher, {"v": value})

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

    def exists(self) -> bool:
        """Whether the graph exists, checked without querying it (a query creates it)."""
        if self._db is None:
            return True
        return self.graph_name in self._db.list_graphs()

    def has_files(self) -> bool:
        if not self.exists():
            return False
        rows = self.query("MATCH (f:File) RETURN count(f)")
        return bool(rows and int(rows[0][0]) > 0)

    def save(self) -> None:
        """No-op for FalkorDB (writes are already persistent)."""

    # --- named reads (shared surface with EmbeddedGraphStore) ---------------

    _RECORD = "s.stable_key, " + ", ".join(f"s.{f}" for f in _SYMBOL_FIELDS)

    def find_symbols(self, name: str) -> list[dict]:
        """Symbols whose name or qualified name is `name` (e.g. "save" or "Db.save")."""
        rows = self.query(
            f"MATCH (s:Function) WHERE s.name = $name OR s.qualname = $name RETURN {self._RECORD} "
            f"UNION ALL MATCH (s:Class) WHERE s.name = $name OR s.qualname = $name RETURN {self._RECORD}",
            {"name": name},
        )
        return sorted((_record(r) for r in rows), key=lambda r: (r["path"], r["line"], r["key"]))

    def get_symbol(self, key: str) -> dict | None:
        rows = self.query(
            f"MATCH (s:Function {{stable_key: $key}}) RETURN {self._RECORD} "
            f"UNION ALL MATCH (s:Class {{stable_key: $key}}) RETURN {self._RECORD}",
            {"key": key},
        )
        return _record(rows[0]) if rows else None

    def symbols_in_file(self, path: str) -> list[dict]:
        rows = self.query(
            f"MATCH (s)-[:DEFINED_IN]->(:File {{path: $path}}) RETURN {self._RECORD}",
            {"path": path},
        )
        return sorted((_record(r) for r in rows), key=lambda r: (r["line"], r["key"]))

    def incoming_calls(self, key: str) -> list[dict]:
        return self._edges(
            "MATCH (c:Function)-[r:CALLS]->(t {stable_key: $key}) "
            "RETURN " + self._RECORD.replace("s.", "c.") + ", r.line, r.confidence, r.candidates, r.reason",
            key,
            "caller",
        )

    def outgoing_calls(self, key: str) -> list[dict]:
        return self._edges(
            "MATCH (f:Function {stable_key: $key})-[r:CALLS]->(c) "
            "RETURN " + self._RECORD.replace("s.", "c.") + ", r.line, r.confidence, r.candidates, r.reason",
            key,
            "callee",
        )

    def call_site_targets(self, caller_key: str, line: int) -> list[dict]:
        """Every target one call site resolved to (the alternatives of an AMBIGUOUS call)."""
        rows = self.query(
            "MATCH (f:Function {stable_key: $key})-[r:CALLS {line: $line}]->(s) RETURN DISTINCT " + self._RECORD,
            {"key": caller_key, "line": line},
        )
        return sorted((_record(r) for r in rows), key=lambda r: (r["path"], r["line"]))

    def file_importers(self, path: str) -> list[str]:
        rows = self.query("MATCH (a:File)-[:IMPORTS_FILE]->(:File {path: $path}) RETURN DISTINCT a.path", {"path": path})
        return sorted(r[0] for r in rows)

    def call_confidence_counts(self) -> dict[str, int]:
        rows = self.query("MATCH ()-[r:CALLS]->() RETURN r.confidence, count(r)")
        return {str(confidence): int(count) for confidence, count in rows}

    def top_connected_symbols(self, count: int = 3) -> list[str]:
        rows = self.query(
            """
            MATCH (:Function)-[:CALLS]->(s)
            WHERE coalesce(s.is_test, false) = false
            WITH s.qualname AS name, count(*) AS degree
            WITH name, sum(degree) AS total
            ORDER BY total DESC, name ASC
            RETURN name LIMIT $count
            """,
            {"count": count},
        )
        return [r[0] for r in rows]

    def find_symbol(self, name: str) -> list[dict]:
        return [
            {"name": r["qualname"], "kind": r["kind"], "path": r["path"], "line": r["line"], "signature": r["signature"]}
            for r in self.find_symbols(name)
        ]

    def _edges(self, cypher: str, key: str, label: str) -> list[dict]:
        out = []
        for row in self.query(cypher, {"key": key}):
            width = len(_SYMBOL_FIELDS) + 1
            line, confidence, candidates, reason = row[width:]
            out.append(
                {
                    label: _record(row[:width]),
                    "line": int(line),
                    "confidence": confidence,
                    "candidates": int(candidates or 1),
                    "reason": reason or "",
                }
            )
        return sorted(out, key=lambda e: (e[label]["path"], e[label]["line"], e["line"]))


if TYPE_CHECKING:
    from graphora.embedded import EmbeddedGraphStore

    # Either backend: they implement the same method surface.
    GraphBackend = Union[GraphStore, EmbeddedGraphStore]


def _symbol_row(symbol: SymbolRecord) -> dict:
    props = {f: getattr(symbol, f) for f in _SYMBOL_FIELDS}
    return {"key": symbol.key, "props": props, "signature_hash": _hash(symbol.signature)}


def _record(row: list) -> dict:
    record = {"key": row[0], **dict(zip(_SYMBOL_FIELDS, row[1:]))}
    record["line"] = int(record["line"] or 0)
    record["end_line"] = int(record["end_line"] or record["line"])
    record["is_test"] = bool(record["is_test"])
    record["returns"] = record.get("returns") or ""
    record["attributes"] = record.get("attributes") or ""
    return record


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
