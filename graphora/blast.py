"""Deterministic blast radius: pure Cypher, zero LLM.

This replaces the old GraphRAG chat-based context building. For every
changed symbol we read its definition, callers, callees, importers, and
covering tests straight from the graph, each fact tagged with the edge
confidence it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from graphora.parser import extract_files_from_diff, extract_symbols_from_diff
from graphora.store import GraphStore


@dataclass(frozen=True)
class SymbolImpact:
    name: str
    kind: str
    path: str
    line: int
    signature: str
    risk_score: float
    fix_count: int
    last_broke_at: str
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
            if impact.fix_count:
                lines.append(
                    f"- RISK MEMORY: involved in {impact.fix_count} fix/revert commits"
                    f" (last: {impact.last_broke_at}), risk score {impact.risk_score:.2f}"
                )
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
                    "risk_score": s.risk_score,
                    "fix_count": s.fix_count,
                    "last_broke_at": s.last_broke_at,
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


def blast_radius(store: GraphStore, symbol_names: list[str]) -> BlastRadius:
    """Compute the blast radius for a list of symbol names."""
    impacts: list[SymbolImpact] = []
    unresolved: list[str] = []
    for name in symbol_names:
        definitions = store.find_definitions(name)
        if not definitions:
            unresolved.append(name)
            continue
        for row in definitions:
            impacts.append(_impact_for(store, row))
    return BlastRadius(symbols=impacts, changed_files=[], unresolved=unresolved)


def blast_radius_for_diff(store: GraphStore, diff: str) -> BlastRadius:
    """Compute the blast radius for a unified diff."""
    symbols = extract_symbols_from_diff(diff)
    radius = blast_radius(store, symbols)
    return BlastRadius(
        symbols=radius.symbols,
        changed_files=extract_files_from_diff(diff),
        unresolved=radius.unresolved,
    )


def _impact_for(store: GraphStore, row: list) -> SymbolImpact:
    name, kind, path, line, signature, risk_score, fix_count, last_broke_at = row
    callers = sorted(store.callers_of(name, path))
    callees = sorted(store.callees_of(name, path))
    tests = sorted(store.tests_covering(name, path))
    importers = sorted(store.importers_of_module(_module_hint(path), path))
    return SymbolImpact(
        name=name,
        kind=kind,
        path=path,
        line=int(line),
        signature=signature or "",
        risk_score=float(risk_score or 0.0),
        fix_count=int(fix_count or 0),
        last_broke_at=str(last_broke_at or ""),
        callers=callers,
        callees=callees,
        tests=tests,
        importers=importers,
    )


def _module_hint(path: str) -> str:
    stem = path.rsplit("/", 1)[-1]
    for suffix in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java"):
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem
