# Graphora Benchmarks

Accuracy and context-size benchmarks for Graphora. Every number is deterministic and
reproducible: no LLM, no network, no tokenizer dependency.

Last updated: 2026-10-16. Environment: macOS (arm64), Python 3.12, embedded backend, plus
FalkorDB (`falkordb/falkordb:latest`) for the backend timing.

## What is measured

`graphora benchmark PATH` reports, for an indexed repository:

1. **Definition coverage.** Python's own `ast` module lists every `def`, `async def`, and
   `class` in the repository's `.py` files, with qualified names (`Outer.Inner.method`,
   `func.nested`). Coverage is the share of those that exist as graph nodes with the same
   qualified name in the same file. A missing definition is a function whose callers Graphora
   can never report.
2. **Call-edge certainty.** The share of `CALLS` edges tagged `EXTRACTED` (the source names the
   target: same file, `self`, an import, a declared or constructed type), `INFERRED` (a bare call
   whose name matches exactly one definition), and `AMBIGUOUS` (several candidates, or an unknown
   receiver type). "Certain" below means `EXTRACTED` + `INFERRED`. Ambiguous edges are not
   errors: they are the calls Graphora reports as "X or Y" instead of guessing.
3. **Context size.** For a few query symbols (the most-connected non-test functions, chosen
   deterministically; pin them with `--symbols`), the prompt tokens an AI reviewer needs under
   three strategies, at a fixed 4 characters per token:
   - *whole-repo dump*: every indexed file;
   - *relevant files*: the files holding the symbols, their callers, and their tests, which is
     what a reviewer must read to learn the same facts;
   - *Graphora*: the blast-radius facts as text.

Context size measures cost, not correctness; accuracy is what items 1 and 2 are for.

## Results

Local checkouts at the listed commits:

```bash
graphora index PATH --project NAME --backend embedded
graphora benchmark PATH --project NAME --backend embedded
```

| Repository | Commit | Files | Definitions found (vs `ast`) | EXTRACTED | INFERRED | AMBIGUOUS | Certain |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Shiftory | `f4e1a03630` | 61 | 498/498 (100%) | 1,367 | 73 | 39 | 97% |
| falkordb-py | `e4230e1ce7` | 42 | 390/390 (100%) | 657 | 0 | 486 | 57% |
| GraphRAG-SDK | `93b760edd4` | 190 | 5,043/5,043 (100%) | 9,415 | 241 | 5,000 | 66% |

| Repository | Whole repo | Relevant files | Graphora | Saving vs repo | vs relevant files |
| --- | ---: | ---: | ---: | ---: | ---: |
| Shiftory | 134,155 | 45,491 | **1,243** | 99.1% | 97.3% |
| falkordb-py | 61,931 | 38,988 | **1,708** | 97.2% | 95.6% |
| GraphRAG-SDK | 888,919 | 692,757 | **36,312** | 95.9% | 94.8% |

### Where the ambiguity comes from

- **falkordb-py** ships a sync and an async twin of most classes (`FalkorDB`/`asyncio.FalkorDB`,
  `Graph`/`AsyncGraph`). Calls are resolved to the right twin when the code states the type
  (`db = FalkorDB(...)`, an import, a parameter annotation, a declared return type such as
  `select_graph() -> Graph`). Most remaining ambiguous calls are made on pytest fixtures, whose
  types are injected at runtime and never written down.
- **GraphRAG-SDK** is built around interchangeable providers (LLMs, embedders, stores) that
  implement the same methods: a call through the base type genuinely could run any of them.

### Correctness spot checks (Shiftory, against the source)

| Question | Graphora | Ground truth (grep/AST) |
| --- | --- | --- |
| Who calls `build_evidence`? | `analyze` + 1 test | same |
| Who calls `analyze`? | 2 callers + 12 tests | same (14 distinct callers) |
| Who calls `cli.main` / `worker.main`? | 2 tests / 1 test | same |
| Call edges into the 8 functions named `main` | 14, all `EXTRACTED`, each to the right `main` | 14 call sites (before import resolution: 112 edges, 98 of them false) |

### Indexing time (GraphRAG-SDK, 190 files)

| Backend | 0.2.1 | 0.3.0 |
| --- | ---: | ---: |
| Embedded | 5.6 s | 2.3 s |
| FalkorDB | 26.5 s | 2.5 s |

The old cost was a per-symbol `splitlines()` (quadratic on large files), a linear edge scan per
call, one JSON rewrite per file, and one Cypher round-trip per row with no indexes. Large files
make the difference much bigger: a 6.6 MB generated parser took 74% of a 126 s index.

## Public benchmark (pinned OSS repositories)

`scripts/public_benchmark.py` clones flask, requests, click, and falkordb-py at pinned commits
and prints the same accuracy and context columns for each, so anyone can reproduce them:

```bash
python3 scripts/public_benchmark.py --output public-benchmark.md
```

It needs network access to clone the repositories.

## Determinism

The suite asserts two consecutive benchmark runs are identical
(`tests/core/test_core_benchmark.py::test_benchmark_is_deterministic`), and that the embedded
and FalkorDB backends produce the same blast radius
(`tests/core/test_core_embedded.py::test_embedded_matches_falkordb_output`).
