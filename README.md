<div align="center">

# Graphora

### Know what breaks before you change it.

**Every function, method, and class in one code graph · What could break, split into certain and uncertain · Zero Docker · $0 LLM cost to build**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/graphora-kg?color=3775A9&logo=pypi&logoColor=white)](https://pypi.org/project/graphora-kg/)
[![FalkorDB](https://img.shields.io/badge/FalkorDB-graph-FF4438?logo=redis&logoColor=white)](https://www.falkordb.com/)
[![tree--sitter](https://img.shields.io/badge/tree--sitter-parsing-2C2C2C)](https://tree-sitter.github.io/tree-sitter/)
[![MCP](https://img.shields.io/badge/MCP-server-6E56CF)](https://modelcontextprotocol.io/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

`pip install` it as a library · run it as a CLI · plug it into any AI agent as an MCP server · or install the skill into 22 agents with one command

</div>

---

> **TL;DR**: Graphora parses a repository with tree-sitter (no LLM) into a graph of every
> function, method, and class, and the calls between them. Ask it what could break if you
> change something, and it answers in three parts: callers that **will** break, callers that
> **might** break (with the alternatives spelled out: "this call is either X or Y"), and the
> tests that reach the change. It never guesses silently.

## Why Graphora

AI coding agents and code review share the same blind spot: they see text, not structure. A
one-line change to a shared function looks harmless in a diff; its callers live in other files.
And when a tool *does* follow calls, it usually picks one target by name and presents the guess
as fact.

| Problem | What Graphora does |
| --- | --- |
| Callers live outside the diff | Walks the call graph backwards (callers of callers, `--depth N`) from every changed function, method, or class. Edits inside a body count. |
| Name matching guesses wrong | Resolves each call the way the language does: `self`/`this` and base classes, same file, imports (incl. re-exports, `src/` layouts, `go.mod`, `Cargo.toml`), same Go package, declared types, field types, and return types. Library calls (`os.path.join`, `fmt.Println`, `String::new()`, `items.append()`) never link to your code. |
| Guesses look like facts | When a call could mean several definitions, it says so: **"this `save(...)` call is either `Db.save` or `FileStore.save`, or a method outside this repository"**. |
| Missing definitions | Every function, method, and class: 100% of the definitions Python's own `ast` finds, on every repo we measured (see [Benchmarks](#benchmarks)). |
| Tests hide in callbacks | `describe`/`it`/`test` blocks, Rust `#[test]`, and `e2e/` page objects count as tests, so the answer lists the tests to run. |
| Deleted code | A diff that deletes a function reports the call sites that still call it. |
| Repo dumps cost tokens | The answer is a few hundred tokens of facts, not the files. |

## Quick start

```bash
pip install graphora-kg          # or: pip install -e . inside this repo

graphora index /path/to/repo     # build the graph (offline, $0)
cd /path/to/repo

graphora breaks charge_card      # what could break if I change charge_card?
graphora breaks Db.save --depth 3
graphora breaks app/db.py:Db.save   # pick one of several same-named definitions
git diff | graphora breaks       # ... for everything my diff touches
```

Without a FalkorDB server, Graphora uses its embedded backend (pure Python, JSON under
`~/.graphora`). For a shared, queryable graph, start FalkorDB and Graphora picks it up:

```bash
docker run -d --name graphora-falkordb -p 6379:6379 falkordb/falkordb:latest
# force a backend anytime with --backend embedded|falkordb|auto
```

## What the answer looks like

Real output, from Graphora indexing its own repository:

```text
$ graphora breaks EmbeddedGraphStore.find_symbols --depth 1

What could break if you change `EmbeddedGraphStore.find_symbols` (graphora/embedded.py:233)

Will break (1):
  - `EmbeddedGraphStore.find_symbol` (graphora/embedded.py:294) calls `EmbeddedGraphStore.find_symbols` at line 297

Might break (1): the call could mean something else
  - `select_symbols` (graphora/blast.py:86) may call `EmbeddedGraphStore.find_symbols` at line 97
      this `find_symbols(...)` call is either `EmbeddedGraphStore.find_symbols` (graphora/embedded.py:233)  <- the one you are changing
      or `GraphStore.find_symbols` (graphora/store.py:278)

Tests that reach it (0):
  none found: consider adding one
```

`select_symbols` receives a store typed `GraphBackend = Union[GraphStore, EmbeddedGraphStore]`,
so the graph cannot know which `find_symbols` runs; it shows both instead of picking one. With
the default `--depth 2` the same question also reaches the CLI commands, the MCP tools, and the
tests behind them. Use `--json` for machine-readable output.

### How certain is each link?

Every `CALLS` edge carries a confidence tag and the rule that produced it:

| Tag | Meaning | Examples |
| --- | --- | --- |
| `EXTRACTED` | The source names the target. | same-file call, `self.method()`, `from pkg.mod import f; f()`, `db = FalkorDB(); db.query()`, `def f(g: Graph): g.query()` |
| `INFERRED` | A bare call whose name matches exactly one definition. | `compute()` with one `compute` in the repo and no import |
| `AMBIGUOUS` | Several definitions match, or the receiver's type is unknown. All candidates are linked, and the answer lists them. | `store.save()` with `Db.save` and `FileStore.save` |

"Will break" uses only `EXTRACTED`/`INFERRED` paths. A single `AMBIGUOUS` call on the path
moves the caller to "might break".

## Four ways to use it

### 1. CLI

```text
graphora index PATH [--include-generated]   Build the graph (no LLM; honours .gitignore, skips generated code)
graphora breaks NAME... [--depth N]         What could break: will / might (X or Y) / tests
graphora breaks --diff F | --git-range R    ... for every symbol a diff touches
graphora review [--diff F|--git-range R]    Same facts as a review, plus missing-test warnings (LLM optional via --llm)
graphora blast NAME...                      Raw callers / callees / tests / importers
graphora stats                              Node and edge counts
graphora benchmark PATH                     Accuracy + context-size benchmark
graphora serve-mcp                          Serve the graph to AI agents over MCP
graphora install-skill [all|AGENT]          Install the Graphora skill for 22 AI coding agents
graphora sessions ingest|connected          Agent session-history graph (see below)
```

Every graph command accepts `--project`, `--backend auto|falkordb|embedded`, `--host`, `--port`.

### 2. Python library

```python
from graphora import index_repository, open_store
from graphora.impact import what_could_break, what_could_break_for_diff

store = open_store("myrepo", backend="embedded")
index_repository("/path/to/repo", project="myrepo", store=store)

result = what_could_break(store, ["Db.save"], depth=2)
print(result.to_text())            # or result.to_dict()

# CI gate: fail when a PR changes code that certainly breaks callers and no test reaches it.
impact = what_could_break_for_diff(store, pr_diff)
assert impact.tests or not impact.will_break, impact.to_text()
```

### 3. MCP server (for AI agents)

```bash
graphora serve-mcp --project myrepo
```

Tools: `what_could_break`, `what_could_break_for_diff`, `blast_radius`, `review_diff`,
`find_symbol`, `graph_stats`. The server tells agents to call `what_could_break` before
editing a symbol and to report uncertain callers as uncertain. Works with mcp 1.x and 2.x.

### 4. One-command skill install (22 agents)

```bash
graphora install-skill all          # every supported agent, into the current repo
graphora install-skill claude-code cursor copilot
graphora install-skill --list       # see all supported agents
```

Writes the Graphora workflow (ask what could break before editing, check the diff before
committing) into each agent's project config: `.claude/skills/`, `.cursor/rules/`,
`.github/instructions/`, `AGENTS.md`, `GEMINI.md`, Windsurf, Cline, Roo, Continue, Amazon Q,
Zed, Goose, Aider, and more. Idempotent; existing files are preserved, shared files get a
marked block.

## What gets indexed

Python, JavaScript/JSX, TypeScript/TSX, Go, Java, Rust, C, C++, Ruby, PHP (tree-sitter,
regex fallback). Per language this includes, for example: Python nested functions, lambdas,
decorated and async defs; JS/TS arrow functions, class fields, object-literal methods,
interfaces, enums; Go methods attached to their receiver type; Java constructors, records,
enums, interfaces; Rust `impl` methods; C++ out-of-class `A::f` definitions and destructors;
Ruby singleton methods; PHP traits and enums. Also CommonJS exports, `export default`
functions and classes, class expressions, `$`/`#private` names, C++ operators and template
specialisations, union type aliases, and identifiers in any script. Each symbol has a
qualified name (`geo.Square.area`), a parent, base classes, and a line range.

Indexing honours `.gitignore` inside a git work tree and skips generated files (ANTLR,
protobuf, `@generated`/`DO NOT EDIT` headers, minified JS) unless `--include-generated`.
`go.mod` and `Cargo.toml` are read to tell the repository's own imports from libraries.

## Agent session memory: graph your work, not just your code

AI coding agents already record every session locally: GitHub Copilot CLI
(`~/.copilot/session-store.db`), Claude Code (`~/.claude/projects/`), Codex CLI
(`~/.codex/sessions/`). Graphora ingests them all into **one graph**, so sessions from
different terminals and different agents become connected the moment they touch the
same file, repo, or PR.

```bash
graphora sessions ingest --days 7             # all agents found (or --source copilot|claude|codex)
graphora sessions connected file build.yml    # which windows/agents touched this file?
graphora sessions connected ref 275           # which sessions relate to PR 275?
graphora sessions connected repo org/proj     # everything that happened in one repo
graphora install-skill all --skill sessions   # let agents run these themselves
```

Read-only on the sources, no LLM, idempotent. See [use case 6](USECASES.md).

## Benchmarks

Measured with `graphora benchmark` on each repository's committed code at the listed commit
(embedded backend, deterministic, no network). "Definitions found" compares the graph with
Python's own `ast` parser (Python files only); "certain" is the share of call edges tagged
`EXTRACTED` or `INFERRED`.

| Repository | Languages | Commit | Files | Definitions found (vs `ast`) | Call edges | Certain | Context: repo → Graphora |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| [Shiftory](https://github.com/Naseem77/Shiftory) | Python, TS | `f4e1a03630` | 61 | 498/498 (100%) | 1,474 | 99% | 134,155 → 1,250 tokens |
| [falkordb-py](https://github.com/FalkorDB/falkordb-py) | Python | `e4230e1ce7` | 42 | 390/390 (100%) | 1,135 | 58% | 61,931 → 1,714 tokens |
| [GraphRAG-SDK](https://github.com/FalkorDB/GraphRAG-SDK) | Python | `93b760edd4` | 190 | 5,038/5,038 (100%) | 13,026 | 75% | 888,503 → 38,273 tokens |
| [QueryWeaver](https://github.com/FalkorDB/QueryWeaver) | Python, TS/TSX | `f37a7d9d84` | 180 | 1,228/1,228 (100%) | 2,766 | 84% | 347,816 → 2,595 tokens |
| [falkordb-ts](https://github.com/FalkorDB/falkordb-ts) | TypeScript | `d3a354209a` | 75 | n/a | 1,871 | 39% | 65,311 → 6,551 tokens |
| [text-to-cypher](https://github.com/FalkorDB/text-to-cypher) | Rust | `1480ccc504` | 31 | n/a | 471 | 88% | 92,730 → 739 tokens |

The remaining ambiguous edges are mostly real polymorphism (several classes implement the
same method, e.g. GraphRAG-SDK's providers) and values whose type the source never states
(pytest fixtures, variables shared across `beforeEach` and test callbacks in falkordb-ts);
those are exactly the calls the answer lists as "X or Y". Indexing GraphRAG-SDK (190 files)
takes 1.7 s embedded and 2.5 s on FalkorDB. Methodology and the pinned public benchmark (`scripts/public_benchmark.py`)
are in [BENCHMARKS.md](./BENCHMARKS.md).

## Architecture

```text
   source files ──▶ tree-sitter parse: symbols (qualified names, ranges, return types),
                    call sites (callee + receiver), import bindings, local type facts
                                            │
                                            ▼
                    resolver (graphora/resolve.py): self → same file → imports →
                    package → declared types → name; external imports dropped
                                            │
                                            ▼
                       code graph (FalkorDB or embedded JSON)
              File · Function · Class · Module  /  CALLS · DEFINED_IN · IMPORTS · IMPORTS_FILE
                       (every CALLS edge: confidence, candidates, reason)
                                            │
              ┌──────────────┬──────────────┼───────────────┬─────────────┐
              ▼              ▼              ▼               ▼             ▼
       what could break   library API     MCP server     benchmark    skill installer
       (CLI, review)      (CI gates)      (AI agents)    (accuracy)   (22 agents)
```

Both stores keep each file's raw call sites and re-resolve the whole graph after every
write, so an incremental update (`update_files`) never drops callers in unchanged files.

## Enterprise notes

- **Privacy**: indexing and queries are fully local. Nothing leaves the machine unless the
  optional `review --llm` is used, and then only the diff plus the graph facts go to *your*
  configured provider.
- **Determinism**: identical input produces identical graphs and answers. No embeddings, no
  model drift in the core.
- **Multi-project**: one FalkorDB instance serves many project graphs (`graphora:{project}`).
  Solo developers need no server at all (embedded backend).
- **Auditability**: every claim in an answer traces to a tagged edge with the rule that
  produced it (`same-file`, `import`, `self`, `returns`, `name`, `receiver-unknown`, ...).

## Testing

```bash
pip install -e ".[dev,mcp]"
python -m pytest tests -q           # 251 tests
ruff check graphora tests scripts && mypy graphora
```

The suite covers every language's definitions (exact sets, including no false definitions),
each resolution rule, what-could-break (certain vs uncertain, depth, diffs, X-or-Y text), both
backends with a parity test, incremental updates, the CLI, the MCP server on mcp 1.x and 2.x,
the skill installer, and the benchmarks. CI runs it against a live FalkorDB and fails on any
skipped test.

## Requirements

| Requirement | Version | Notes |
| --- | --- | --- |
| Python | 3.10+ | |
| FalkorDB | optional | for the client-server backend; embedded backend needs nothing |
| git | any | `.gitignore` handling and `--git-range` |
| litellm | optional | only for `review --llm` |
| mcp | optional | only for `serve-mcp` (1.x or 2.x) |

## License

MIT
