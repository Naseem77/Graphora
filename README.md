<div align="center">

# Graphora

### The code knowledge graph that reviews your changes.

**Deterministic indexing · Blast-radius analysis · Risk memory · $0 LLM cost to build**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FalkorDB](https://img.shields.io/badge/FalkorDB-graph-FF4438?logo=redis&logoColor=white)](https://www.falkordb.com/)
[![tree--sitter](https://img.shields.io/badge/tree--sitter-parsing-2C2C2C)](https://tree-sitter.github.io/tree-sitter/)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF)](https://modelcontextprotocol.io/)
[![Tests](https://img.shields.io/badge/tests-41%20passing-brightgreen)](#testing)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

`pip install` it as a library · run it as a CLI · plug it into any AI agent as an MCP server

</div>

---

> **TL;DR**: Graphora keeps a live structural graph of your codebase in FalkorDB, built with tree-sitter and zero LLM calls. When code changes, it reads the real blast radius (callers, callees, tests, importers) straight from the graph, remembers where the codebase broke before (risk memory mined from git history), and grounds every review claim in tagged, auditable facts. Measured result: **90 to 93% fewer prompt tokens** than repo dumping, with *more* accurate reviews.

## Why Graphora

AI code review and AI coding agents share the same blind spot: they see text, not structure.

| Problem | What goes wrong |
| --- | --- |
| **Blind to blast radius** | A one-line change to a shared function looks harmless in isolation. Its callers, tests, and dependents live in other files, outside the diff. |
| **Expensive to compensate** | The usual fix is stuffing more of the repo into the prompt. That burns tokens and still buries the signal in noise. |
| **Hard to trust** | Without grounding, the model guesses. Reviewers cannot tell a grounded claim from a hallucination. |
| **No memory of pain** | Every review starts from zero. The tool does not know that this exact function was hotfixed twice and reverted once. |

Graphora fixes all four at the source:

| Capability | How |
| --- | --- |
| **Live structural graph** | `File`, `Function`, `Class`, `Module` nodes; `CALLS`, `DEFINED_IN`, `IMPORTS` edges. Built deterministically with tree-sitter, updated incrementally. |
| **Blast radius, not repo dumps** | Pure Cypher queries return exactly the callers, callees, covering tests, and importers of a change. Hundreds of tokens instead of tens of thousands. |
| **Confidence tags on every fact** | `EXTRACTED` (explicit in source), `INFERRED` (single-candidate name resolution), `AMBIGUOUS` (multiple candidates, all linked and flagged). You always know what was read versus guessed. |
| **Risk memory** | Git history mining links fix, hotfix, and revert commits to the exact symbols they touched. Each symbol carries `fix_count`, `last_broke_at`, and a `risk_score` that decays as the area stays quiet. Reviews warn on historically fragile code automatically. |
| **$0 index cost** | No LLM, no embeddings, no network in the index path. Code never leaves your machine unless *you* enable the optional LLM review pass. |

## Quick start

```bash
# 1. Graph database (one container)
docker run -d --name graphora-falkordb -p 6379:6379 falkordb/falkordb:latest

# 2. Install
pip install -e .            # inside this repo; installs the `graphora` CLI

# 3. Build the graph and the risk memory (both offline, both $0)
graphora index /path/to/repo
graphora risk mine /path/to/repo

# 4. Use it
graphora blast my_function                 # who calls it, who tests it, did it break before
graphora risk top                          # the riskiest symbols in the codebase
graphora review --git-range main...HEAD --repo /path/to/repo
graphora benchmark /path/to/repo           # reproduce the token-cost numbers yourself
```

## Three ways to consume it

### 1. CLI

```text
graphora index PATH                 Build the graph for a repository (no LLM)
graphora blast SYMBOL [...]         Blast radius: callers, callees, tests, risk
graphora review [--diff F|--git-range R]   Grounded diff review (LLM optional via --llm)
graphora risk mine PATH             Mine git fix/revert history into the graph
graphora risk top                   Riskiest symbols, ranked
graphora benchmark PATH             Reproducible token-cost benchmark
graphora serve-mcp                  Serve the graph to AI agents over MCP
graphora stats                      Node and edge counts
```

### 2. Python library

```python
from graphora import GraphStore, index_repository, blast_radius
from graphora.blast import blast_radius_for_diff
from graphora.risk import mine_risk_memory, risk_report

store = index_repository("/path/to/repo")          # deterministic build
mine_risk_memory(store, "/path/to/repo")           # teach it the history

# CI gate: block changes to called-but-untested symbols, three lines
radius = blast_radius_for_diff(store, pr_diff)
untested = [s.name for s in radius.symbols if s.callers and not s.tests]
assert not untested, f"Changed symbols with callers but no tests: {untested}"
```

### 3. MCP server (for AI agents)

```bash
graphora serve-mcp --project myrepo
```

Exposes `blast_radius`, `review_diff`, `risk_top`, `find_symbol`, and `graph_stats` to Copilot CLI, Claude Code, Cursor, or any MCP client. The server instructs agents to check the blast radius *before* editing a symbol, which turns Graphora into a guardrail for AI-generated changes.

## Risk memory: the graph learns where the codebase breaks

Graphora mines git history (deterministically, zero LLM) for fix, hotfix, bugfix, and revert commits, and attributes each one to the exact functions and classes it touched, using both diff lines and hunk-header context.

```
risk_score = (1 - 0.7^fix_count) * 0.5^(months_since_last_fix / 6)
```

A symbol fixed often and recently scores near 1.0. A quiet area decays by half every 6 months. Reviews then carry findings no stateless tool can produce:

> **risk-memory** `falkordb/asyncio/cluster.py:9`: `Is_Cluster` was involved in 2 past fix/revert commits (last: 2025-03-13), risk score 0.08, 2 callers. Review extra carefully.

The longer Graphora runs on a repository, the smarter it gets. That compounds.

## Benchmarks

Measured on real repositories, fully deterministic, zero network. Full methodology and reproduction commands in [BENCHMARKS.md](./BENCHMARKS.md).

| Repository | Whole-repo dump | Relevant files | **Graphora blast radius** | Savings |
| --- | ---: | ---: | ---: | ---: |
| falkordb-py (39 files) | 52,432 tokens | 34,224 tokens | **3,439 tokens** | **93.4%** |
| Graphora itself (17 files) | 22,160 tokens | 16,603 tokens | **1,237 tokens** | **94.4%** |

Graph build cost in LLM credits: **$0**, by construction. The "relevant files" column is the honest baseline: every file a reviewer would have to read to learn what the blast radius states directly.

## Use cases

Five real, captured scenarios in [USECASES.md](./USECASES.md):

1. **Cross-file impact review with zero LLM**: a one-line diff, callers and covering tests read straight from the graph, ~197 tokens of context.
2. **Risk hotspot mining on a real codebase**: 231 commits of falkordb-py history, the async cluster code correctly surfaced as the trouble spot.
3. **AI agents over MCP**: a live stdio session calling `blast_radius`, `risk_top`, `find_symbol`.
4. **A CI gate in three lines** of library code, plus incremental re-indexing.
5. **Ambiguity honesty**: duplicate symbol names tagged `AMBIGUOUS` instead of silently guessed.

## Architecture

```text
                        ┌────────────────────────────────────────────┐
   source files ───────▶│  tree-sitter parse (deterministic, $0)     │
   git history  ───────▶│  risk miner (fix/revert attribution, $0)   │
                        └───────────────────┬────────────────────────┘
                                            ▼
                              FalkorDB code graph
                 File · Function · Class · Module · FixCommit
              CALLS / DEFINED_IN / IMPORTS / FIXED  (confidence-tagged)
                                            │
              ┌──────────────┬──────────────┼───────────────┐
              ▼              ▼              ▼               ▼
           CLI           library API     MCP server     benchmark
        (terminal)      (CI gates,      (AI agents)    (reproducible)
                         refactors)
```

Graph model per project: `graphora:{project}`. Incremental updates re-parse only changed files. The optional LLM review pass (litellm, any provider) receives the diff plus the compact blast radius, never the repository.

## Enterprise notes

- **Privacy**: the index path is fully local. No code, no metadata leaves the machine unless the optional `--llm` review is enabled, and then only the diff plus a few hundred tokens of graph facts are sent to *your* configured provider.
- **Determinism**: identical input produces identical graphs, reviews, and benchmark numbers. No embeddings drift, no model version drift in the core.
- **Multi-project**: one FalkorDB instance serves many project graphs (`graphora:{project}`), so a single shared server can back a whole team.
- **Auditability**: every review claim traces to a tagged graph fact; every risk score traces to specific commits (`FixCommit` nodes with sha, date, subject).
- **Incremental cost**: re-index only changed files on push; risk mining is idempotent per commit.

## Testing

41 tests, all passing: parser, store, blast radius, risk memory, CLI, MCP server, and benchmark. Integration tests run against a live FalkorDB and real scripted git repositories, and skip cleanly when FalkorDB is absent.

```bash
python3 -m pytest tests -q        # 41 passed
```

The test-per-phase methodology and the bugs the suite caught are documented in [USECASES.md](./USECASES.md#how-it-was-tested).

## Requirements

| Requirement | Version | Notes |
| --- | --- | --- |
| Python | 3.10+ | |
| FalkorDB | any recent | `docker run -p 6379:6379 falkordb/falkordb` |
| git | any | for risk memory mining |
| litellm | optional | only for `review --llm` |
| mcp | optional | only for `serve-mcp` |

Languages indexed today: Python, JavaScript, TypeScript, Go, Java (tree-sitter grammars, regex fallback).

## License

MIT
