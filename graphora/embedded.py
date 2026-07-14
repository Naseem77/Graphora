"""Embedded graph store: pure Python, JSON on disk, zero Docker.

Implements the same method surface as `graphora.store.GraphStore`, so every
consumer (indexer, blast radius, risk memory, review, CLI, MCP, benchmark)
works unchanged. Data persists to `{data_dir}/{project}.json`
(default `~/.graphora`, override with GRAPHORA_DATA_DIR).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from graphora.parser import AMBIGUOUS, INFERRED, ParsedFile

SCHEMA_VERSION = 1


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
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if loaded.get("schema_version") == SCHEMA_VERSION:
                    self._data = loaded
            except (json.JSONDecodeError, OSError):
                pass

    @staticmethod
    def _empty() -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "files": {},        # path -> {language, is_test, content_hash, line_count, parser}
            "symbols": {},      # stable_key -> {name, kind, path, line, signature, confidence,
                                #                is_test, pending_calls, fix_count, last_broke_at, risk_score}
            "calls": [],        # {a, b, line, confidence} (a, b = stable keys)
            "imports": [],      # {path, module, confidence}
            "fix_commits": {},  # sha -> {date, subject, kind}
            "touched": [],      # [sha, path]
            "fixed": [],        # [sha, stable_key]
        }

    @property
    def graph_name(self) -> str:
        return f"graphora:{self.project}"

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self._path)

    # --- writes ---------------------------------------------------------

    def write_files(self, parsed_files: list[ParsedFile]) -> int:
        for parsed in parsed_files:
            self._delete_file_no_save(parsed.path)
            self._data["files"][parsed.path] = {
                "language": parsed.language,
                "is_test": parsed.is_test,
                "content_hash": _hash(parsed.source_text),
                "line_count": len(parsed.source_text.splitlines()),
                "parser": parsed.parser_used,
            }
            for imp in parsed.imports:
                self._data["imports"].append(
                    {"path": parsed.path, "module": imp.name, "confidence": imp.confidence}
                )
            for symbol in parsed.symbols:
                kind = "Function" if symbol.kind == "Function" else "Class"
                key = f"{parsed.path}:{kind}:{symbol.name}"
                self._data["symbols"][key] = {
                    "name": symbol.name,
                    "kind": kind,
                    "path": parsed.path,
                    "line": symbol.line,
                    "signature": symbol.signature,
                    "confidence": symbol.confidence,
                    "is_test": parsed.is_test,
                    "pending_calls": [],
                    "fix_count": 0,
                    "last_broke_at": "",
                    "risk_score": 0.0,
                }
            local_functions = {s.name for s in parsed.symbols if s.kind == "Function"}
            for call in parsed.calls:
                caller_key = f"{parsed.path}:Function:{call.caller}"
                if call.callee in local_functions:
                    self._merge_call(caller_key, f"{parsed.path}:Function:{call.callee}", call.line, call.confidence)
                elif caller_key in self._data["symbols"]:
                    self._data["symbols"][caller_key]["pending_calls"].append(f"{call.callee}@{call.line}")
        self.save()
        return len(parsed_files)

    def link_cross_file_calls(self) -> int:
        by_function_name: dict[str, list[str]] = {}
        for key, sym in self._data["symbols"].items():
            if sym["kind"] == "Function":
                by_function_name.setdefault(sym["name"], []).append(key)
        linked = 0
        for key, sym in self._data["symbols"].items():
            pending = sym.get("pending_calls") or []
            if not pending:
                continue
            for entry in pending:
                callee_name, line = entry.rsplit("@", 1)
                candidates = by_function_name.get(callee_name, [])
                if not candidates:
                    continue
                confidence = INFERRED if len(candidates) == 1 else AMBIGUOUS
                for callee_key in candidates:
                    if callee_key == key:
                        continue
                    self._merge_call(key, callee_key, int(line), confidence)
                    linked += 1
            sym["pending_calls"] = []
        self.save()
        return linked

    def _merge_call(self, a: str, b: str, line: int, confidence: str) -> None:
        for edge in self._data["calls"]:
            if edge["a"] == a and edge["b"] == b and edge["line"] == line:
                edge["confidence"] = confidence
                return
        self._data["calls"].append({"a": a, "b": b, "line": line, "confidence": confidence})

    def _delete_file_no_save(self, path: str) -> None:
        self._data["files"].pop(path, None)
        dead = {k for k, s in self._data["symbols"].items() if s["path"] == path}
        for key in dead:
            del self._data["symbols"][key]
        self._data["calls"] = [e for e in self._data["calls"] if e["a"] not in dead and e["b"] not in dead]
        self._data["imports"] = [i for i in self._data["imports"] if i["path"] != path]
        self._data["touched"] = [t for t in self._data["touched"] if t[1] != path]
        self._data["fixed"] = [f for f in self._data["fixed"] if f[1] not in dead]

    def delete_file(self, path: str) -> None:
        self._delete_file_no_save(path)
        self.save()

    def clear(self) -> None:
        self._data = self._empty()
        self.save()

    def delete_graph(self) -> None:
        self._data = self._empty()
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
            + len(self._data["symbols"])  # DEFINED_IN
            + len(self._data["touched"])
            + len(self._data["fixed"]),
        }

    def has_files(self) -> bool:
        return bool(self._data["files"])

    def find_definitions(self, name: str) -> list[list]:
        return [
            [s["name"], s["kind"], s["path"], s["line"], s["signature"],
             s.get("risk_score", 0.0), s.get("fix_count", 0), s.get("last_broke_at", "")]
            for s in self._sorted_symbols()
            if s["name"] == name
        ]

    def callers_of(self, name: str, path: str) -> list[tuple[str, str, str]]:
        return self._call_neighbors(name, path, direction="in", tests=False)

    def callees_of(self, name: str, path: str) -> list[tuple[str, str, str]]:
        return self._call_neighbors(name, path, direction="out")

    def tests_covering(self, name: str, path: str) -> list[tuple[str, str, str]]:
        return self._call_neighbors(name, path, direction="in", tests=True)

    def _call_neighbors(self, name: str, path: str, direction: str, tests: bool | None = None):
        key = f"{path}:Function:{name}"
        seen: set[tuple[str, str, str]] = set()
        out: list[tuple[str, str, str]] = []
        for edge in self._data["calls"]:
            other_key = None
            if direction == "in" and edge["b"] == key:
                other_key = edge["a"]
            elif direction == "out" and edge["a"] == key:
                other_key = edge["b"]
            if other_key is None:
                continue
            other = self._data["symbols"].get(other_key)
            if other is None or other["kind"] != "Function":
                continue
            if tests is not None and bool(other.get("is_test", False)) != tests:
                continue
            row = (other["name"], other["path"], edge["confidence"])
            if row not in seen:
                seen.add(row)
                out.append(row)
        return out

    def importers_of_module(self, module_hint: str, exclude_path: str) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for imp in self._data["imports"]:
            if imp["module"].endswith(module_hint) and imp["path"] != exclude_path and imp["path"] not in seen:
                seen.add(imp["path"])
                out.append(imp["path"])
        return out

    def top_connected_symbols(self, count: int = 3) -> list[str]:
        degree: dict[str, int] = {}
        for edge in self._data["calls"]:
            callee = self._data["symbols"].get(edge["b"])
            if callee and callee["kind"] == "Function" and not callee.get("is_test", False):
                degree[callee["name"]] = degree.get(callee["name"], 0) + 1
        ranked = sorted(degree.items(), key=lambda kv: (-kv[1], kv[0]))
        return [name for name, _ in ranked[:count]]

    def find_symbol(self, name: str) -> list[dict]:
        return [
            {"name": s["name"], "kind": s["kind"], "path": s["path"],
             "line": int(s["line"] or 0), "signature": s["signature"]}
            for s in self._sorted_symbols()
            if s["name"] == name
        ]

    def _sorted_symbols(self) -> list[dict]:
        return [self._data["symbols"][k] for k in sorted(self._data["symbols"])]

    # --- risk memory surface -------------------------------------------------

    def known_fix_shas(self) -> set[str]:
        return set(self._data["fix_commits"])

    def upsert_fix_commit(self, sha: str, date: str, subject: str, kind: str) -> None:
        self._data["fix_commits"][sha] = {"date": date, "subject": subject[:300], "kind": kind}
        self.save()

    def touch_file(self, sha: str, path: str) -> int:
        if path not in self._data["files"] or sha not in self._data["fix_commits"]:
            return 0
        if [sha, path] not in self._data["touched"]:
            self._data["touched"].append([sha, path])
        self.save()
        return 1

    def record_symbol_fix(self, path: str, name: str, sha: str, date: str) -> int:
        if sha not in self._data["fix_commits"]:
            return 0
        matched = 0
        for key in sorted(self._data["symbols"]):
            sym = self._data["symbols"][key]
            if sym["path"] != path or sym["name"] != name:
                continue
            if [sha, key] not in self._data["fixed"]:
                self._data["fixed"].append([sha, key])
            sym["fix_count"] = int(sym.get("fix_count", 0)) + 1
            if (sym.get("last_broke_at") or "") < date:
                sym["last_broke_at"] = date
            matched += 1
        if matched:
            self.save()
        return matched

    def risky_symbols(self, limit: int = 15) -> list[list]:
        rows = []
        for key in sorted(self._data["symbols"]):
            sym = self._data["symbols"][key]
            if int(sym.get("fix_count", 0)) <= 0:
                continue
            callers = {
                e["a"]
                for e in self._data["calls"]
                if e["b"] == key
                and (c := self._data["symbols"].get(e["a"])) is not None
                and c["kind"] == "Function"
            }
            rows.append(
                [sym["name"], sym["path"], sym["line"], int(sym["fix_count"]),
                 sym.get("last_broke_at", ""), float(sym.get("risk_score", 0.0)), len(callers)]
            )
        rows.sort(key=lambda r: (-r[5], -r[3]))
        return rows[:limit]

    def symbols_with_fixes(self) -> list[list]:
        return [
            [s["path"], s["name"], int(s["fix_count"]), s.get("last_broke_at", "")]
            for s in self._sorted_symbols()
            if int(s.get("fix_count", 0)) > 0
        ]

    def set_risk_score(self, path: str, name: str, score: float) -> None:
        changed = False
        for sym in self._data["symbols"].values():
            if sym["path"] == path and sym["name"] == name:
                sym["risk_score"] = score
                changed = True
        if changed:
            self.save()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
