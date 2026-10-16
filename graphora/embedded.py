"""Embedded graph store: pure Python, JSON on disk, zero Docker.

Implements the same method surface as `graphora.store.GraphStore`, so every
consumer (indexer, blast radius, review, CLI, MCP, benchmark) works
unchanged. Data persists to `{data_dir}/{project}.json` (default
`~/.graphora`, override with GRAPHORA_DATA_DIR).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

from graphora.parser import ParsedFile
from graphora.resolve import CallSite, FileRecord, SymbolRecord, records_for, resolve

# 2: qualified-name symbol keys, raw call sites per file, resolved file imports.
SCHEMA_VERSION = 2


def default_data_dir() -> Path:
    return Path(os.environ.get("GRAPHORA_DATA_DIR", str(Path.home() / ".graphora")))


class EmbeddedGraphStore:
    """A named code graph for one project, held in memory and saved as JSON."""

    backend = "embedded"

    def __init__(self, project: str, data_dir: str | Path | None = None, **_ignored):
        self.project = project
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self._path = self.data_dir / f"{project}.json"
        self._data = self._empty()
        self.stale_schema = False
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if loaded.get("schema_version") == SCHEMA_VERSION:
                    self._data = loaded
                else:
                    self.stale_schema = True  # older Graphora layout: re-index to rebuild
            except (json.JSONDecodeError, OSError):
                pass
        self._reindex()

    @staticmethod
    def _empty() -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "files": {},  # path -> {language, is_test, content_hash, line_count, parser, bindings, calls}
            "symbols": {},  # key -> SymbolRecord fields
            "calls": [],  # {a, b, line, confidence, candidates, reason} (a, b = symbol keys)
            "imports": [],  # {path, module, confidence}
            "file_imports": [],  # [importer path, imported path]
            "sessions": {},  # sid -> {summary, cwd, branch, created_at, updated_at, last_ask}
            "session_repos": [],  # [sid, repo]
            "session_files": [],  # {sid, path, tool}
            "session_refs": [],  # [sid, kind, value]
        }

    def _reindex(self) -> None:
        """Rebuild the in-memory adjacency indexes from the persisted edge lists."""
        self._incoming: dict[str, list[dict]] = defaultdict(list)
        self._outgoing: dict[str, list[dict]] = defaultdict(list)
        for edge in self._data["calls"]:
            self._incoming[edge["b"]].append(edge)
            self._outgoing[edge["a"]].append(edge)
        self._by_file: dict[str, list[str]] = defaultdict(list)
        for key, symbol in self._data["symbols"].items():
            self._by_file[symbol["path"]].append(key)

    @property
    def graph_name(self) -> str:
        return f"graphora:{self.project}"

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self._path)

    # --- writes ---------------------------------------------------------

    def write_files(self, parsed_files: list[ParsedFile], save: bool = True) -> int:
        """Store files, their symbols, and raw call sites. Call
        link_cross_file_calls() afterwards to (re)build the CALLS edges."""
        for parsed in parsed_files:
            self._delete_file_no_save(parsed.path)
            symbols, record = records_for(parsed)
            self._data["files"][parsed.path] = {
                "language": parsed.language,
                "is_test": parsed.is_test,
                "content_hash": _hash(parsed.source_text),
                "line_count": parsed.source_text.count("\n") + (0 if parsed.source_text.endswith("\n") else 1),
                "parser": parsed.parser_used,
                "bindings": [list(b) for b in record.bindings],
                "calls": [[c.caller_key, c.callee, c.receiver, c.line] for c in record.calls],
            }
            for imp in parsed.imports:
                self._data["imports"].append({"path": parsed.path, "module": imp.name, "confidence": imp.confidence})
            for symbol in symbols:
                self._data["symbols"][symbol.key] = symbol.to_dict()
                self._by_file[parsed.path].append(symbol.key)
        if save:
            self.save()
        return len(parsed_files)

    def link_cross_file_calls(self) -> int:
        """Resolve every stored call site into CALLS edges (all files, so
        callers in unchanged files relink to callees in changed ones)."""
        result = resolve(self._symbol_records(), self._file_records())
        self._data["calls"] = [dict(edge.__dict__) for edge in result.edges]
        self._data["file_imports"] = [list(pair) for pair in result.file_imports]
        self._reindex()
        self.save()
        return len(result.edges)

    def _symbol_records(self) -> list[SymbolRecord]:
        return [SymbolRecord(**{k: v for k, v in s.items() if k != "key"}, key=key) for key, s in self._data["symbols"].items()]

    def _file_records(self) -> list[FileRecord]:
        return [
            FileRecord(
                path=path,
                language=f["language"],
                is_test=f["is_test"],
                bindings=tuple((*b, "")[:4] for b in f.get("bindings", [])),
                calls=tuple(CallSite(*c) for c in f.get("calls", [])),
            )
            for path, f in self._data["files"].items()
        ]

    def _delete_file_no_save(self, path: str) -> None:
        if self._data["files"].pop(path, None) is None and path not in self._by_file:
            return
        dead = set(self._by_file.pop(path, []))
        for key in dead:
            self._data["symbols"].pop(key, None)
        if dead:
            self._data["calls"] = [e for e in self._data["calls"] if e["a"] not in dead and e["b"] not in dead]
            self._reindex()
        self._data["imports"] = [i for i in self._data["imports"] if i["path"] != path]
        self._data["file_imports"] = [p for p in self._data["file_imports"] if path not in p]

    def delete_file(self, path: str) -> None:
        self._delete_file_no_save(path)
        self.save()

    def clear(self) -> None:
        self._data = self._empty()
        self._reindex()
        self.save()

    def delete_graph(self) -> None:
        self._data = self._empty()
        self._reindex()
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass

    # --- reads ------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        symbols = self._data["symbols"].values()
        calls = len(self._data["calls"])
        return {
            "files": len(self._data["files"]),
            "functions": sum(1 for s in symbols if s["kind"] == "Function"),
            "classes": sum(1 for s in symbols if s["kind"] == "Class"),
            "modules": len({i["module"] for i in self._data["imports"]}),
            "calls": calls,
            "edges": calls
            + len(self._data["imports"])
            + len(self._data["file_imports"])
            + len(self._data["symbols"]),  # DEFINED_IN
        }

    def has_files(self) -> bool:
        return bool(self._data["files"])

    # --- agent sessions (see graphora/sessions.py) -------------------------

    def upsert_session(self, sid: str, props: dict) -> None:
        self._data.setdefault("sessions", {})[sid] = dict(props)

    def link_session_repo(self, sid: str, repo: str) -> None:
        edges = self._data.setdefault("session_repos", [])
        if [sid, repo] not in edges:
            edges.append([sid, repo])

    def link_session_file(self, sid: str, path: str, tool: str) -> None:
        edges = self._data.setdefault("session_files", [])
        for edge in edges:
            if edge["sid"] == sid and edge["path"] == path:
                edge["tool"] = tool
                return
        edges.append({"sid": sid, "path": path, "tool": tool})

    def link_session_ref(self, sid: str, kind: str, value: str) -> None:
        edges = self._data.setdefault("session_refs", [])
        if [sid, kind, value] not in edges:
            edges.append([sid, kind, value])

    def sessions_connected(self, kind: str, value: str) -> list[list]:
        sessions = self._data.get("sessions", {})

        def row(sid: str, via: str) -> list:
            props = sessions.get(sid, {})
            return [sid, props.get("summary", ""), props.get("updated_at", ""), via,
                    props.get("agent", "")]

        if kind == "file":
            hits = [row(e["sid"], e["path"])
                    for e in self._data.get("session_files", [])
                    if e["path"].endswith(value)]
        elif kind == "repo":
            hits = [row(sid, repo)
                    for sid, repo in self._data.get("session_repos", [])
                    if repo == value]
        else:
            hits = [row(sid, f"{k} {v}")
                    for sid, k, v in self._data.get("session_refs", [])
                    if v == value or v.endswith(value)]
        return sorted(hits, key=lambda r: r[2], reverse=True)

    # --- named reads (shared surface with GraphStore) ----------------------

    def find_symbols(self, name: str) -> list[dict]:
        """Symbols whose name or qualified name is `name` (e.g. "save" or "Db.save")."""
        hits = [
            self._record(key)
            for key, s in self._data["symbols"].items()
            if s["name"] == name or s["qualname"] == name
        ]
        return sorted(hits, key=lambda r: (r["path"], r["line"], r["key"]))

    def get_symbol(self, key: str) -> dict | None:
        return self._record(key) if key in self._data["symbols"] else None

    def symbols_in_file(self, path: str) -> list[dict]:
        return sorted((self._record(k) for k in self._by_file.get(path, [])), key=lambda r: (r["line"], r["key"]))

    def incoming_calls(self, key: str) -> list[dict]:
        return self._edges(self._incoming.get(key, []), "a", "caller")

    def outgoing_calls(self, key: str) -> list[dict]:
        return self._edges(self._outgoing.get(key, []), "b", "callee")

    def call_site_targets(self, caller_key: str, line: int) -> list[dict]:
        """Every target one call site resolved to (the alternatives of an AMBIGUOUS call)."""
        keys = [e["b"] for e in self._outgoing.get(caller_key, []) if e["line"] == line]
        return sorted((self._record(k) for k in dict.fromkeys(keys)), key=lambda r: (r["path"], r["line"]))

    def call_sites_named(self, name: str) -> list[dict]:
        """Every stored call site whose callee is `name`, and whether it resolved."""
        out = []
        for f in self._data["files"].values():
            for caller_key, callee, receiver, line in f.get("calls", []):
                if callee != name or caller_key not in self._data["symbols"]:
                    continue
                # Only a certain edge counts: an AMBIGUOUS one may be a same-named survivor.
                resolved = any(
                    e["line"] == line
                    and e["confidence"] != "AMBIGUOUS"
                    and self._data["symbols"].get(e["b"], {}).get("name") == name
                    for e in self._outgoing.get(caller_key, [])
                )
                out.append({"caller": self._record(caller_key), "line": line, "receiver": receiver, "resolved": resolved})
        return sorted(out, key=lambda s: (s["caller"]["path"], s["line"]))

    def file_importers(self, path: str) -> list[str]:
        return sorted({a for a, b in self._data["file_imports"] if b == path})

    def call_confidence_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for edge in self._data["calls"]:
            counts[edge["confidence"]] = counts.get(edge["confidence"], 0) + 1
        return counts

    def top_connected_symbols(self, count: int = 3) -> list[str]:
        degree: dict[str, int] = {}
        for edge in self._data["calls"]:
            callee = self._data["symbols"].get(edge["b"])
            if callee and not callee.get("is_test", False):
                degree[callee["qualname"]] = degree.get(callee["qualname"], 0) + 1
        ranked = sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))
        return [name for name, _ in ranked[:count]]

    def find_symbol(self, name: str) -> list[dict]:
        return [
            {"name": r["qualname"], "kind": r["kind"], "path": r["path"], "line": r["line"], "signature": r["signature"]}
            for r in self.find_symbols(name)
        ]

    def _record(self, key: str) -> dict:
        return {"key": key, **self._data["symbols"][key]}

    def _edges(self, edges: list[dict], end: str, label: str) -> list[dict]:
        out = []
        for edge in edges:
            if edge[end] not in self._data["symbols"]:
                continue
            out.append(
                {
                    label: self._record(edge[end]),
                    "line": edge["line"],
                    "confidence": edge["confidence"],
                    "candidates": edge.get("candidates", 1),
                    "reason": edge.get("reason", ""),
                }
            )
        return sorted(out, key=lambda e: (e[label]["path"], e[label]["line"], e["line"]))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
