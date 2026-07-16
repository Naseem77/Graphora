<div align="center">

# Graphora

### The code knowledge graph that reviews your changes.

**Deterministic indexing · Blast-radius analysis · Risk memory · Zero Docker required · $0 LLM cost to build**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/graphora-kg?color=3775A9&logo=pypi&logoColor=white)](https://pypi.org/project/graphora-kg/)
[![FalkorDB](https://img.shields.io/badge/FalkorDB-graph-FF4438?logo=redis&logoColor=white)](https://www.falkordb.com/)
[![tree--sitter](https://img.shields.io/badge/tree--sitter-parsing-2C2C2C)](https://tree-sitter.github.io/tree-sitter/)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF)](https://modelcontextprotocol.io/)
[![Tests](https://img.shields.io/badge/tests-80%20passing-brightgreen)](#testing)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

<img src="assets/banner.gif" alt="Graphora: source files flow into a live knowledge graph that powers blast radius, diff review with risk memory, and MCP tools for agents. 22,160 repo tokens shrink to 1,237." width="900" />

`pip install` it as a library · run it as a CLI · plug it into any AI agent as an MCP server · or install the skill into 22 agents with one command

</div>

---

> **TL;DR**: Graphora keeps a live structural graph of your codebase, built with tree-sitter and zero LLM calls, stored in FalkorDB or in a zero-setup embedded backend. When code changes, it reads the real blast radius (callers, callees, tests, importers) straight from the graph, remembers where the codebase broke before (risk memory mined from git history), and grounds every review claim in tagged, auditable facts. Measured result: **82 to 94% fewer prompt tokens** than repo dumping on real pinned OSS repos, with *more* accurate reviews.

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
| **Zero-setup or client-server** | Works instantly with an embedded pure-Python backend (JSON on disk, no Docker), or against a shared FalkorDB server for live Cypher and multi-project teams. Auto-detected. |
| **11 languages** | Python, JavaScript, TypeScript (+JSX/TSX), Go, Java, Rust, C, C++, Ruby, PHP. Tree-sitter first, regex fallback. |
| **$0 index cost** | No LLM, no embeddings, no network in the index path. Code never leaves your machine unless *you* enable the optional LLM review pass. |

## Quick start

```bash
# 1. Install (no server, no Docker needed)
pip install graphora-kg     # from PyPI, or: pip install -e . inside this repo

# 2. Build the graph and the risk memory (both offline, both $0)
graphora index /path/to/repo
graphora risk mine /path/to/repo

# 3. Use it
graphora blast my_function                 # who calls it, who tests it, did it break before
graphora risk top                          # the riskiest symbols in the codebase
graphora review --git-range main...HEAD --repo /path/to/repo
graphora benchmark /path/to/repo           # reproduce the token-cost numbers yourself
```

Zero configuration: without a FalkorDB server Graphora uses its embedded backend (pure Python, JSON at `~/.graphora`). For live Cypher queries and team-shared graphs, start FalkorDB and Graphora picks it up automatically:

```bash
docker run -d --name graphora-falkordb -p 6379:6379 falkordb/falkordb:latest
# force a backend anytime with --backend embedded|falkordb|auto
```

## Four ways to consume it

### 1. CLI

```text
graphora index PATH                 Build the graph for a repository (no LLM)
graphora blast SYMBOL [...]         Blast radius: callers, callees, tests, risk
graphora review [--diff F|--git-range R]   Grounded diff review (LLM optional via --llm)
graphora risk mine PATH             Mine git fix/revert history into the graph
graphora risk top                   Riskiest symbols, ranked
graphora benchmark PATH             Reproducible token-cost benchmark
graphora serve-mcp                  Serve the graph to AI agents over MCP
graphora install-skill [all|AGENT]  Install the Graphora skill for 22 AI coding agents
graphora stats                      Node and edge counts
```

Every graph command accepts `--backend auto|falkordb|embedded` (default: auto).

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

### 4. One-command skill install (22 agents)

```bash
graphora install-skill all          # or: graphora install-skill claude-code cursor copilot
graphora install-skill --list      # see all supported agents
```

Writes the Graphora workflow rule (check blast radius before editing, run a grounded review before committing, consult risk memory) into each agent's project config: `.claude/skills/`, `.cursor/rules/`, `.github/instructions/`, `AGENTS.md`, `GEMINI.md`, Windsurf, Cline, Roo, Continue, Amazon Q, Zed, Goose, Aider, and more. Idempotent; existing files are preserved, shared files get a marked block.

## Risk memory: the graph learns where the codebase breaks

Graphora mines git history (deterministically, zero LLM) for fix, hotfix, bugfix, and revert commits, and attributes each one to the exact functions and classes it touched, using both diff lines and hunk-header context.

```
risk_score = (1 - 0.7^fix_count) * 0.5^(months_since_last_fix / 6)
```

A symbol fixed often and recently scores near 1.0. A quiet area decays by half every 6 months. Reviews then carry findings no stateless tool can produce:

> **risk-memory** `falkordb/asyncio/cluster.py:9`: `Is_Cluster` was involved in 2 past fix/revert commits (last: 2025-03-13), risk score 0.08, 2 callers. Review extra carefully.

The longer Graphora runs on a repository, the smarter it gets. That compounds.

## Agent session memory: graph your work, not just your code

If you use the GitHub Copilot CLI across many terminal tabs, it already records every
session locally (`~/.copilot/session-store.db`): summaries, files touched, PR references.
Graphora ingests that as another data source, so sessions from different terminals become
connected the moment they touch the same file, repo, or PR:

```bash
graphora sessions ingest --days 7             # load your session history into the graph

graphora sessions connected file build.yml    # which windows touched this file?
graphora sessions connected ref 275           # which sessions relate to PR 275?
graphora sessions connected repo org/proj     # everything that happened in one repo
```

No FalkorDB container? Same commands work with the embedded JSON backend:

```bash
graphora sessions ingest --days 7 --backend embedded
graphora sessions connected file build.yml --backend embedded
```

Read-only on the source, no LLM, idempotent. See [use case 6](USECASES.md).

## Benchmarks

Measured on real repositories, fully deterministic, zero network. Full methodology and reproduction commands in [BENCHMARKS.md](./BENCHMARKS.md).

Public benchmark on pinned OSS commits, reproducible with `python3 scripts/public_benchmark.py`:

| Repository | Commit | Whole-repo dump | Relevant files | **Graphora blast radius** | Savings |
| --- | --- | ---: | ---: | ---: | ---: |
| flask | `36e4a824f3` | 147,390 tokens | 124,205 tokens | **21,534 tokens** | **85.4%** |
| requests | `f361ead047` | 101,739 tokens | 80,360 tokens | **10,331 tokens** | **89.8%** |
| click | `b67832c216` | 225,373 tokens | 190,236 tokens | **39,534 tokens** | **82.5%** |
| falkordb-py | `971f1d124e` | 59,866 tokens | 40,638 tokens | **3,550 tokens** | **94.1%** |

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
                       code graph (FalkorDB or embedded)
                 File · Function · Class · Module · FixCommit
              CALLS / DEFINED_IN / IMPORTS / FIXED  (confidence-tagged)
                                            │
              ┌──────────────┬──────────────┼───────────────┬─────────────┐
              ▼              ▼              ▼               ▼             ▼
           CLI           library API     MCP server     benchmark    skill installer
        (terminal)      (CI gates,      (AI agents)    (reproducible)  (22 agents)
                         refactors)
```

Graph model per project: `graphora:{project}` in FalkorDB, or `~/.graphora/{project}.json` embedded. Incremental updates re-parse only changed files. The optional LLM review pass (litellm, any provider) receives the diff plus the compact blast radius, never the repository.

## Enterprise notes

- **Privacy**: the index path is fully local. No code, no metadata leaves the machine unless the optional `--llm` review is enabled, and then only the diff plus a few hundred tokens of graph facts are sent to *your* configured provider.
- **Determinism**: identical input produces identical graphs, reviews, and benchmark numbers. No embeddings drift, no model version drift in the core.
- **Multi-project**: one FalkorDB instance serves many project graphs (`graphora:{project}`), so a single shared server can back a whole team. Solo developers need no server at all (embedded backend).
- **Auditability**: every review claim traces to a tagged graph fact; every risk score traces to specific commits (`FixCommit` nodes with sha, date, subject).
- **Incremental cost**: re-index only changed files on push; risk mining is idempotent per commit.

## Testing

80 tests, all passing: parser (11 languages), FalkorDB store, embedded store (including backend-parity tests), blast radius, risk memory, CLI, MCP server, skill installer, and benchmarks. Integration tests run against a live FalkorDB and real scripted git repositories, and skip cleanly when FalkorDB is absent.

```bash
python3 -m pytest tests -q        # 80 passed
```

The test-per-phase methodology and the bugs the suite caught are documented in [USECASES.md](./USECASES.md#how-it-was-tested).

## Requirements

| Requirement | Version | Notes |
| --- | --- | --- |
| Python | 3.10+ | |
| FalkorDB | optional | for the client-server backend; embedded backend needs nothing |
| git | any | for risk memory mining |
| litellm | optional | only for `review --llm` |
| mcp | optional | only for `serve-mcp` |

Languages indexed today: Python, JavaScript, TypeScript, Go, Java, Rust, C, C++, Ruby, PHP (tree-sitter grammars, regex fallback).

## License

MIT
