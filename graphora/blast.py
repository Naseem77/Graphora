"""Deterministic blast radius: pure Cypher, zero LLM.

This replaces the old GraphRAG chat-based context building. For every
changed symbol we read its definition, callers, callees, importers, and
covering tests straight from the graph, each fact tagged with the edge
confidence it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from graphora.parser import extract_files_from_diff

if TYPE_CHECKING:
    from graphora.store import GraphBackend


@dataclass(frozen=True)
class SymbolImpact:
    name: str
    kind: str
    path: str
    line: int
    signature: str
    callers: list[tuple[str, str, str]] = field(default_factory=list)  # (name, path, confidence)
    callees: list[tuple[str, str, str]] = field(default_factory=list)
    tests: list[tuple[str, str, str]] = field(default_factory=list)
    importers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BlastRadius:
    symbols: list[SymbolImpact]
    changed_files: list[str]
    unresolved: list[str]

    def to_context(self) -> str:
        """Compact, LLM-ready textual context (a few hundred tokens, not a repo dump)."""
        lines: list[str] = ["Blast radius (from code graph, deterministic):"]
        for impact in self.symbols:
            lines.append(f"\n### {impact.name} ({impact.kind})")
            lines.append(f"- defined at {impact.path}:{impact.line} `{impact.signature}`")
            lines.append(_relation_line("callers", impact.callers))
            lines.append(_relation_line("calls", impact.callees))
            lines.append(_relation_line("tests", impact.tests))
            if impact.importers:
                lines.append(f"- imported by ({len(impact.importers)}): {', '.join(impact.importers)}")
        if self.unresolved:
            lines.append(f"\nSymbols not found in graph: {', '.join(self.unresolved)}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "changed_files": self.changed_files,
            "unresolved": self.unresolved,
            "symbols": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "path": s.path,
                    "line": s.line,
                    "signature": s.signature,
                    "callers": [{"name": n, "path": p, "confidence": c} for n, p, c in s.callers],
                    "callees": [{"name": n, "path": p, "confidence": c} for n, p, c in s.callees],
                    "tests": [{"name": n, "path": p, "confidence": c} for n, p, c in s.tests],
                    "importers": s.importers,
                }
                for s in self.symbols
            ],
        }


def _relation_line(label: str, rows: list[tuple[str, str, str]]) -> str:
    if not rows:
        return f"- {label} (0): none"
    rendered = ", ".join(f"{name} [{path}] ({confidence})" for name, path, confidence in rows)
    return f"- {label} ({len(rows)}): {rendered}"


def blast_radius(store: GraphBackend, symbol_names: list[str]) -> BlastRadius:
    """Compute the blast radius for symbol names (plain or qualified, e.g. "Db.save")."""
    impacts: list[SymbolImpact] = []
    unresolved: list[str] = []
    for name in symbol_names:
        records = store.find_symbols(name)
        if not records:
            unresolved.append(name)
            continue
        impacts.extend(_impact_for(store, record) for record in records)
    return BlastRadius(symbols=impacts, changed_files=[], unresolved=unresolved)


def blast_radius_for_diff(store: GraphBackend, diff: str) -> BlastRadius:
    """Compute the blast radius for a unified diff: every symbol a hunk touches,
    including edits inside a body (not only changed definition lines)."""
    from graphora.impact import changed_symbols

    records, unresolved = changed_symbols(store, diff)
    return BlastRadius(
        symbols=[_impact_for(store, record) for record in records],
        changed_files=extract_files_from_diff(diff),
        unresolved=unresolved,
    )


def members_of(store: GraphBackend, record: dict) -> list[dict]:
    """A symbol plus, for a class, everything defined inside it (its methods)."""
    if record["kind"] != "Class":
        return [record]
    prefix = record["qualname"] + "."
    inner = [
        s
        for s in store.symbols_in_file(record["path"])
        if s["qualname"].startswith(prefix) and record["line"] <= s["line"] <= record["end_line"]
    ]
    return [record, *inner]


def _impact_for(store: GraphBackend, record: dict) -> SymbolImpact:
    targets = members_of(store, record)
    own = {t["key"] for t in targets}
    callers: set[tuple[str, str, str]] = set()
    tests: set[tuple[str, str, str]] = set()
    callees: set[tuple[str, str, str]] = set()
    for target in targets:
        for edge in store.incoming_calls(target["key"]):
            caller = edge["caller"]
            if caller["key"] in own:
                continue
            row = (caller["qualname"], caller["path"], edge["confidence"])
            (tests if caller["is_test"] else callers).add(row)
        for edge in store.outgoing_calls(target["key"]):
            callee = edge["callee"]
            if callee["key"] not in own:
                callees.add((callee["qualname"], callee["path"], edge["confidence"]))
    return SymbolImpact(
        name=record["qualname"],
        kind=record["kind"],
        path=record["path"],
        line=int(record["line"]),
        signature=record["signature"] or "",
        callers=sorted(callers),
        callees=sorted(callees),
        tests=sorted(tests),
        importers=store.file_importers(record["path"]),
    )
