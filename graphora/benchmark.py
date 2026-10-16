"""Reproducible benchmark. Deterministic, zero LLM, zero network.

Accuracy first:

- definition coverage: every def/class Python's own `ast` module finds in
  the repository's .py files must be a node in the graph (by qualified name);
- call-edge certainty: how many CALLS edges the source states explicitly
  (EXTRACTED), match a unique name (INFERRED), or stay ambiguous.

Then context size. For a set of query symbols (auto-picked
highest-connectivity functions by default), compares the prompt context an
AI reviewer would need under three strategies:

1. whole-repo dump (the context-stuffing anti-pattern)
2. equivalent-information file set: the changed files plus every file
   containing a caller or covering test (what a reviewer must actually
   read to learn what the blast radius states directly)
3. graphora blast radius (facts read from the graph)

Token counts use a fixed 4-chars-per-token heuristic so runs are
reproducible on any machine with no tokenizer dependency.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from graphora.blast import blast_radius
from graphora.indexer import collect_source_files
from graphora.review import estimate_tokens

if TYPE_CHECKING:
    from graphora.store import GraphBackend


@dataclass(frozen=True)
class BenchmarkResult:
    project: str
    symbols: list[str]
    file_count: int
    repo_tokens: int
    changed_files_tokens: int
    blast_tokens: int
    grounded_facts: dict[str, int]
    python_definitions: int = 0  # def/class nodes Python's ast finds
    python_definitions_found: int = 0  # of those, present in the graph
    call_confidence: dict[str, int] = field(default_factory=dict)

    @property
    def definition_coverage(self) -> float:
        if not self.python_definitions:
            return 100.0
        return 100.0 * self.python_definitions_found / self.python_definitions

    @property
    def saving_vs_repo(self) -> float:
        if self.repo_tokens == 0:
            return 0.0
        return 100.0 * (1 - self.blast_tokens / self.repo_tokens)

    @property
    def saving_vs_changed_files(self) -> float:
        if self.changed_files_tokens == 0:
            return 0.0
        return 100.0 * (1 - self.blast_tokens / self.changed_files_tokens)

    def render(self) -> str:
        lines = [
            "# Graphora benchmark",
            "",
            f"Project: `{self.project}` ({self.file_count} indexed source files)",
            f"Query symbols: {', '.join(f'`{s}`' for s in self.symbols)}",
            "",
            "## Accuracy",
            "",
            f"- Definitions in the graph: **{self.python_definitions_found:,}/{self.python_definitions:,}**"
            f" Python functions, methods, and classes ({self.definition_coverage:.1f}%), checked against"
            " Python's own `ast` parser.",
            f"- Call edges: {_confidence_line(self.call_confidence)}",
            "",
            "## Context size",
            "",
            "| Context strategy | Prompt tokens | Relative |",
            "| --- | ---: | ---: |",
            f"| Whole-repo dump | {self.repo_tokens:,} | 100% |",
            f"| Relevant files (changed + callers + tests) | {self.changed_files_tokens:,} |"
            f" {100.0 * self.changed_files_tokens / max(1, self.repo_tokens):.1f}% |",
            f"| **Graphora blast radius** | **{self.blast_tokens:,}** |"
            f" **{100.0 * self.blast_tokens / max(1, self.repo_tokens):.2f}%** |",
            "",
            f"Savings: **{self.saving_vs_repo:.1f}%** vs whole-repo dump,"
            f" **{self.saving_vs_changed_files:.1f}%** vs reading the relevant files.",
            "",
            "Grounded facts included in the blast-radius context (a repo dump has these",
            "only implicitly, buried in the noise):",
            "",
            f"- caller relationships: {self.grounded_facts['callers']}",
            f"- callee relationships: {self.grounded_facts['callees']}",
            f"- covering tests identified: {self.grounded_facts['tests']}",
            "",
            "_Token counts use a fixed 4 chars/token heuristic; the benchmark is fully",
            "deterministic (no LLM, no network) and reproducible with:_",
            "`graphora benchmark <path> --symbols " + ",".join(self.symbols) + "`",
        ]
        return "\n".join(lines)


def pick_symbols(store: GraphBackend, count: int = 3) -> list[str]:
    """Deterministically pick the most-connected non-test functions."""
    return store.top_connected_symbols(count)


def run_benchmark(
    store: GraphBackend,
    root: str | Path,
    symbols: list[str] | None = None,
) -> BenchmarkResult:
    root = Path(root).resolve()
    files = collect_source_files(root)
    repo_tokens = 0
    contents: dict[str, str] = {}
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(root))
        contents[rel] = text
        repo_tokens += estimate_tokens(text)

    symbols = symbols or pick_symbols(store)
    if not symbols:
        raise RuntimeError("No symbols to benchmark; index the repository first.")

    radius = blast_radius(store, symbols)
    blast_tokens = estimate_tokens(radius.to_context())

    # Fair baseline: to learn what the blast radius states, a reviewer must
    # read the changed files plus every file holding a caller or covering test.
    changed_paths = {impact.path for impact in radius.symbols}
    for impact in radius.symbols:
        changed_paths.update(path for _, path, _ in impact.callers)
        changed_paths.update(path for _, path, _ in impact.tests)
    changed_files_tokens = sum(
        estimate_tokens(contents[p]) for p in changed_paths if p in contents
    )

    grounded = {
        "callers": sum(len(i.callers) for i in radius.symbols),
        "callees": sum(len(i.callees) for i in radius.symbols),
        "tests": sum(len(i.tests) for i in radius.symbols),
    }

    total, found = _python_definition_coverage(store, contents)
    return BenchmarkResult(
        project=store.project,
        symbols=symbols,
        file_count=len(files),
        repo_tokens=repo_tokens,
        changed_files_tokens=changed_files_tokens,
        blast_tokens=blast_tokens,
        grounded_facts=grounded,
        python_definitions=total,
        python_definitions_found=found,
        call_confidence=store.call_confidence_counts(),
    )


def _python_definition_coverage(store: GraphBackend, contents: dict[str, str]) -> tuple[int, int]:
    """(definitions Python's ast finds, how many of them the graph has), by qualified name."""
    total = found = 0
    for path, text in sorted(contents.items()):
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            continue
        truth: set[str] = set()
        _collect_definitions(tree, "", truth)
        graph = {s["qualname"] for s in store.symbols_in_file(path)}
        total += len(truth)
        found += len(truth & graph)
    return total, found


def _collect_definitions(node: ast.AST, prefix: str, out: set[str]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualname = prefix + child.name
            out.add(qualname)
            _collect_definitions(child, qualname + ".", out)
        else:
            _collect_definitions(child, prefix, out)


def _confidence_line(counts: dict[str, int]) -> str:
    total = sum(counts.values()) or 1
    parts = [
        f"{counts.get('EXTRACTED', 0):,} EXTRACTED ({100 * counts.get('EXTRACTED', 0) / total:.0f}%, the source names the target)",
        f"{counts.get('INFERRED', 0):,} INFERRED ({100 * counts.get('INFERRED', 0) / total:.0f}%, unique name)",
        f"{counts.get('AMBIGUOUS', 0):,} AMBIGUOUS ({100 * counts.get('AMBIGUOUS', 0) / total:.0f}%, shown as 'X or Y')",
    ]
    return ", ".join(parts)
