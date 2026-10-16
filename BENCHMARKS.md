# Graphora Benchmarks

Accuracy and context-size benchmarks for Graphora. Every number is deterministic and
reproducible: no LLM, no network, no tokenizer dependency.

Last updated: 2026-10-17. Environment: macOS (arm64), Python 3.12, embedded backend, plus
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

Each repository's committed code at the listed commit (exported with `git archive`, so local
uncommitted work does not count):

```bash
graphora index PATH --project NAME --backend embedded
graphora benchmark PATH --project NAME --backend embedded
```

| Repository | Languages | Commit | Files | Definitions found (vs `ast`) | EXTRACTED | INFERRED | AMBIGUOUS | Certain |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Shiftory | Python, TS | `f4e1a03630` | 61 | 498/498 (100%) | 1,393 | 73 | 8 | 99% |
| falkordb-py | Python | `e4230e1ce7` | 42 | 390/390 (100%) | 660 | 0 | 475 | 58% |
| GraphRAG-SDK | Python | `93b760edd4` | 190 | 5,038/5,038 (100%) | 9,540 | 238 | 3,248 | 75% |
| QueryWeaver | Python, TS/TSX | `f37a7d9d84` | 180 | 1,228/1,228 (100%) | 2,271 | 49 | 446 | 84% |
| falkordb-ts | TypeScript | `d3a354209a` | 75 | n/a (no Python) | 735 | 0 | 1,136 | 39% |
| text-to-cypher | Rust | `1480ccc504` | 31 | n/a (no Python) | 414 | 0 | 57 | 88% |

| Repository | Whole repo | Relevant files | Graphora | Saving vs repo | vs relevant files |
| --- | ---: | ---: | ---: | ---: | ---: |
| Shiftory | 134,155 | 45,491 | **1,250** | 99.1% | 97.3% |
| falkordb-py | 61,931 | 38,988 | **1,714** | 97.2% | 95.6% |
| GraphRAG-SDK | 888,503 | 504,677 | **38,273** | 95.7% | 92.4% |
| QueryWeaver | 347,816 | 105,826 | **2,595** | 99.3% | 97.5% |
| falkordb-ts | 65,311 | 35,917 | **6,551** | 90.0% | 81.8% |
| text-to-cypher | 92,730 | 33,885 | **739** | 99.2% | 97.8% |

### Where the ambiguity comes from

- **falkordb-py** ships a sync and an async twin of most classes (`FalkorDB`/`asyncio.FalkorDB`,
  `Graph`/`AsyncGraph`). Calls are resolved to the right twin when the code states the type
  (`db = FalkorDB(...)`, an import, a parameter annotation, a declared return type such as
  `select_graph() -> Graph`, a dataclass field). Most remaining ambiguous calls are made on
  pytest fixtures, whose types are injected at runtime and never written down.
- **falkordb-ts** tests share untyped variables between `beforeEach` and the test callbacks
  (`let graph; beforeEach(() => { graph = ... })`), so `graph.query()` in a test could be any
  `query`. The tests themselves are found (77 reach `Graph.query`).
- **GraphRAG-SDK** is built around interchangeable providers (LLMs, embedders, stores) that
  implement the same methods: a call through the base type genuinely could run any of them.

### Correctness spot checks (Shiftory, against the source)

| Question | Graphora | Ground truth (grep/AST) |
| --- | --- | --- |
| Who calls `build_evidence`? | `analyze` + 1 test | same |
| Who calls `analyze`? | `_analyze`, `_explain_in_run` + 12 tests | same (14 distinct callers) |
| Who calls `cli.main` / `worker.main`? | 2 tests / 1 test | same |
| Call edges into the 8 functions named `main` | 14, all `EXTRACTED`, each to the right `main` | 14 call sites (before import resolution: 112 edges, 98 of them false) |

### Indexing time (GraphRAG-SDK, 190 files)

| Backend | 0.2.1 | 0.3.0 |
| --- | ---: | ---: |
| Embedded | 2.6 s | 1.7 s |
| FalkorDB | 23.4 s | 2.5 s |

The old cost was a per-symbol `splitlines()` (quadratic on large files), a linear edge scan per
call, one JSON rewrite per file, and one Cypher round-trip per row with no indexes. Large files
make the difference much bigger: a 6.6 MB generated parser took 74% of a 126 s index.

### Other correctness checks (from an independent end-to-end review)

| Scenario | Result |
| --- | --- |
| text-to-cypher (Rust): who calls `TextToCypherClient::new`? | the 4 example callers (certain) + 13 inline `#[test]` functions under tests; no false "might break" (was 83 from `String::new()` & co.) |
| GraphRAG-SDK: who calls `EvidenceLedger.get`? | 1 caller + the 2 tests that call it through a dataclass field (was 263 "might break" + 612 "tests" from `dict.get`) |
| falkordb-py: delete `Graph.drop_edge_vector_index`, then `git diff \| graphora breaks` | reports it as deleted with `test_edge_index_drop` (sync), not the async test that still works |
| QueryWeaver: a Playwright helper used by page objects | 0 "will break", 143 tests through `e2e/` page objects |

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
