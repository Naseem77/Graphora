# Graphora Benchmarks

Token-cost and grounding benchmarks for Graphora's blast-radius context versus the
alternatives an AI reviewer actually has. Every number below is deterministic and
reproducible on any machine: no LLM, no network, no tokenizer dependency.

Last updated: 2026-07-14. Environment: macOS, Python 3.11, FalkorDB (Docker `falkordb/falkordb:latest`).

## Summary

- **82 to 94% fewer prompt tokens** than a whole-repo dump on 4 pinned real OSS repos (flask, requests, click, falkordb-py), reproducible with one script.
- **79 to 92% fewer tokens** even against the honest baseline of reading only the relevant files.
- **$0 LLM credits to build the graph**, on every run, by construction: the index path is tree-sitter plus Cypher, nothing else.
- The blast-radius context is not just smaller, it is *more* informative: caller counts, covering tests, and risk history are stated as explicit facts instead of left for the model to infer from raw text.

## Methodology

For a set of query symbols, the benchmark compares the prompt context an AI reviewer
would need under three strategies:

| Strategy | What it represents |
| --- | --- |
| **Whole-repo dump** | The context-stuffing anti-pattern: paste everything and hope the model finds the signal. |
| **Relevant files** | The honest baseline. The changed files *plus every file containing a caller or covering test*: exactly what a reviewer (human or LLM) must read to learn what the blast radius states directly. |
| **Graphora blast radius** | Definition, callers, callees, covering tests, importers, and risk memory for the query symbols, read from the graph as compact text. |

Rules that keep it fair and reproducible:

- **Fixed token heuristic**: 4 characters per token, identical for every strategy. No tokenizer dependency, so runs are byte-for-byte reproducible.
- **Deterministic symbol selection**: by default the benchmark picks the most-connected non-test functions from the graph (stable ordering: degree desc, name asc). You can pin symbols with `--symbols` for exact reproduction.
- **Same source of truth**: all three strategies read the same indexed file set, with the same ignore rules.
- **Zero LLM, zero network**: the benchmark never calls a model. It measures context size and grounded-fact counts only.
- **Determinism is tested**: the suite asserts two consecutive runs produce identical results (`tests/core/test_core_benchmark.py::test_benchmark_is_deterministic`).

## Results

### Public benchmark: pinned OSS repositories (reproducible by anyone)

Run on real open-source repos at exact pinned commits, embedded backend
(no server), symbols auto-picked deterministically. Two consecutive runs
produce byte-identical reports.

```bash
python3 scripts/public_benchmark.py
```

| Repository | Commit | Files | Whole repo | Relevant files | Blast radius | Savings vs repo | vs relevant |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| flask | `36e4a824f3` | 83 | 147,390 | 124,205 | **21,534** | **85.4%** | 82.7% |
| requests | `f361ead047` | 37 | 101,739 | 80,360 | **10,331** | **89.8%** | 87.1% |
| click | `b67832c216` | 76 | 225,373 | 190,236 | **39,534** | **82.5%** | 79.2% |
| falkordb-py | `971f1d124e` | 40 | 59,866 | 40,638 | **3,550** | **94.1%** | 91.3% |

Symbols queried: flask `get, route, register_blueprint` · requests `get, post, prepare` · click `invoke, command, echo` · falkordb-py `query, select_graph, list_indices`.

### falkordb-py (real client library: 39 files, 310 functions, 943 call edges)

```bash
graphora index . --project falkordb-py
graphora benchmark . --project falkordb-py --symbols query,select_graph,delete
```

| Context strategy | Prompt tokens | Relative |
| --- | ---: | ---: |
| Whole-repo dump | 52,432 | 100% |
| Relevant files (changed + callers + tests) | 34,224 | 65.3% |
| **Graphora blast radius** | **3,439** | **6.56%** |

Savings: **93.4%** vs whole-repo dump, **90.0%** vs relevant files.

Grounded facts stated explicitly in the blast-radius context: 8 caller relationships,
4 callee relationships, 248 covering tests identified.

### Graphora itself (17 files, 148 functions, 251 call edges)

```bash
graphora index . --project graphora
graphora benchmark . --project graphora --symbols query,blast_radius,render
```

| Context strategy | Prompt tokens | Relative |
| --- | ---: | ---: |
| Whole-repo dump | 22,160 | 100% |
| Relevant files (changed + callers + tests) | 16,603 | 74.9% |
| **Graphora blast radius** | **1,237** | **5.58%** |

Savings: **94.4%** vs whole-repo dump, **92.5%** vs relevant files.

Grounded facts stated explicitly in the blast-radius context: 27 caller relationships,
3 callee relationships, 28 covering tests identified.

### Single-change review (the flagship scenario)

A one-line diff adding a parameter to `compute_risk_score()` in this repository:

| Approach | Context to the model |
| --- | ---: |
| Whole-repo dump | ~22,200 tokens |
| **Graphora** (diff + blast radius) | **~197 tokens** |

That 197-token context contained everything a correct review needed: the single caller
identified by name and file, the covering test, and 3 importing files, each fact
confidence-tagged. The review claims were *read* from the graph, not guessed.
See [USECASES.md](./USECASES.md#use-case-1) for the full output.

## Why the "relevant files" baseline matters

Most published token-savings numbers compare against repo dumping, which flatters any
tool. The fair question is: *what would it cost to learn the same facts without the graph?*
The answer is reading every file that contains a caller or a covering test. Graphora beats
that baseline by 90 to 92% because the graph states relationships in one line each
("callers (3): a, b, c") that otherwise hide across thousands of lines of source.

The baseline also *understates* Graphora's advantage: a file dump still leaves the model
to infer the relationships, while the blast radius asserts them with confidence tags. And
none of the alternatives carry risk memory at all, at any token price.

## Cost economics

| Cost | Graphora | Typical LLM-ingest systems |
| --- | --- | --- |
| Graph build | **$0** (tree-sitter + Cypher) | per-document LLM extraction |
| Embeddings | **none** (no vector store) | per-chunk embedding calls |
| Re-index on push | only changed files | often full re-ingest |
| Review context | ~200 to ~3,500 tokens | 30,000+ tokens or external context services |
| Risk memory build | **$0** (git log parsing) | not available |

## Reproducing

```bash
# infra
docker run -d --name graphora-falkordb -p 6379:6379 falkordb/falkordb:latest

# install
pip install graphora-kg

# any repository
graphora index /path/to/repo --project myrepo
graphora benchmark /path/to/repo --project myrepo --output benchmark-report.md

# pin symbols for exact reproduction of the tables above
graphora benchmark . --project falkordb-py --symbols query,select_graph,delete
```

The benchmark is also covered by the test suite (5 dedicated tests, including a
determinism assertion): `python3 -m pytest tests/core/test_core_benchmark.py -q`.
