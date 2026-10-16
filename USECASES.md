# Graphora Use Cases

Real scenarios, executed locally and captured verbatim on 2026-10-17 (use cases 1-5 against
Graphora's own repository and Shiftory; use case 6 against a synthetic session store).
Environment: macOS, Python 3.12, embedded backend (no server) unless noted.

Setup used for use cases 1-5:

```bash
pip install graphora-kg
graphora index . --project graphora --backend embedded   # 37 files, 530 functions, 28 classes, 1,236 call edges
```

---

## Use case 1: review a diff with what it could break, zero LLM <a name="use-case-1"></a>

**Scenario**: a one-line edit *inside the body* of `records_for()`. A diff-only reviewer sees
one line in one file. (The previous release only noticed changed `def` lines, so a body edit
produced "no known symbols changed".)

```bash
graphora review --diff change.diff --project graphora
```

Captured output:

````
# Graphora Review

Changed: `records_for`. Will break: 2 caller(s); might break: 2 (ambiguous calls); tests that reach the change: 56.

## Grounded facts (from the graph)

```
What could break if you change `records_for` (graphora/resolve.py:184)

Will break (2):
  - `EmbeddedGraphStore.write_files` (graphora/embedded.py:88) calls `records_for` at line 93
  - `GraphStore.write_files` (graphora/store.py:78) calls `records_for` at line 88

Might break (2): the call could mean something else
  - `index_repository` (graphora/indexer.py:110) may call `EmbeddedGraphStore.write_files` at line 158 (index_repository -> EmbeddedGraphStore.write_files -> records_for)
      this `write_files(...)` call is either `EmbeddedGraphStore.write_files` (graphora/embedded.py:88)
      or `GraphStore.write_files` (graphora/store.py:78)
      or a method outside this repository
  - `update_files` (graphora/indexer.py:167) may call `EmbeddedGraphStore.write_files` at line 191 (update_files -> EmbeddedGraphStore.write_files -> records_for)
      this `write_files(...)` call is either `EmbeddedGraphStore.write_files` (graphora/embedded.py:88)
      or `GraphStore.write_files` (graphora/store.py:78)
      or a method outside this repository

Tests that reach it (56):
  - `test_star_import_cycles_resolve_quickly` (tests/core/test_core_diff.py:77) calls `records_for` at line 85
  - `_resolve` (tests/core/test_core_resolve.py:7) calls `records_for` at line 10
  - `test_redefined_function_gets_its_own_key` (tests/core/test_core_resolve.py:148) calls `records_for` at line 149
  - `_resolve` (tests/core/test_core_resolve_languages.py:7) calls `records_for` at line 10
  - `_edge_lines` (tests/core/test_core_resolve_languages.py:384) calls `records_for` at line 387
  - `test_import_picks_the_named_module_not_every_same_name_function` (tests/core/test_core_resolve.py:26) calls `_resolve` at line 27 (test_import_picks_the_named_module_not_every_same_name_function -> _resolve -> records_for)
  - `test_src_layout_import_resolves` (tests/core/test_core_resolve.py:39) calls `_resolve` at line 40 (test_src_layout_import_resolves -> _resolve -> records_for)
  - `test_external_import_creates_no_edge` (tests/core/test_core_resolve.py:52) calls `_resolve` at line 53 (test_external_import_creates_no_edge -> _resolve -> records_for)
  - `test_self_call_resolves_to_own_class_method` (tests/core/test_core_resolve.py:63) calls `_resolve` at line 64 (test_self_call_resolves_to_own_class_method -> _resolve -> records_for)
  - `test_unknown_receiver_lists_every_candidate_as_ambiguous` (tests/core/test_core_resolve.py:82) calls `_resolve` at line 83 (test_unknown_receiver_lists_every_candidate_as_ambiguous -> _resolve -> records_for)
  ... and 46 more (use --json for all)
```

_Facts mode: no model was called._
````

**Why it matters**: both store implementations certainly break. The indexer's calls reach
them through a parameter typed as *either* backend, so they are reported as "might break" with
both possibilities named, instead of silently picking one. The tests that exercise the change
(directly, or through helpers like `_resolve`) are listed so you know what to run.

---

## Use case 2: "X or Y" instead of a guess <a name="use-case-2"></a>

**Scenario**: change `EmbeddedGraphStore.find_symbols`. `select_symbols` calls
`store.find_symbols(...)` on a store typed `GraphBackend = Union[GraphStore, EmbeddedGraphStore]`.

```bash
graphora breaks EmbeddedGraphStore.find_symbols --depth 1 --project graphora
```

Captured output:

```
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

**Why it matters**: a name-matching tool would either link every `find_symbols` in sight or
pick one. Graphora tells the agent exactly which call sites are uncertain and what they could
resolve to, so it can check them instead of trusting a guess.

---

## Use case 3: imports beat names (Shiftory) <a name="use-case-3"></a>

**Scenario**: [Shiftory](https://github.com/Naseem77/Shiftory) has 8 functions named `main`
(scripts, a CLI, a worker). Its tests call `cli.main(...)` and `worker.main()` 14 times.

| | Previous release (name matching) | Now (import resolution) |
| --- | --- | --- |
| Edges into the 8 `main` functions | 112 (every call linked to all 8; 98 false) | 14, all `EXTRACTED`, each to the right `main` |
| Who calls `build_evidence`? | correct | `analyze` + 1 test (correct) |
| Who calls `analyze`? | correct | `_analyze`, `_explain_in_run` + 12 tests (correct) |

```bash
graphora breaks build_evidence --project shiftory
```

```
Will break (3):
  - `analyze` (src/shiftory/evidence/builder.py:81) calls `build_evidence` at line 90
  - `_analyze` (src/shiftory/cli.py:161) calls `analyze` at line 163 (_analyze -> analyze -> build_evidence)
  - `_explain_in_run` (src/shiftory/cli.py:430) calls `analyze` at line 457 (_explain_in_run -> analyze -> build_evidence)
```

---

## Use case 4: any AI agent, via MCP <a name="use-case-4"></a>

```bash
graphora serve-mcp --project graphora
```

Tools listed by the server: `what_could_break`, `what_could_break_for_diff`, `blast_radius`,
`review_diff`, `find_symbol`, `graph_stats`. A captured `what_could_break` call with
`{"symbols": ["records_for"], "depth": 1, "as_json": true}` (abridged):

```json
{
 "targets": [{"name": "records_for", "kind": "Function", "path": "graphora/resolve.py", "line": 184}],
 "will_break": [
  {"name": "EmbeddedGraphStore.write_files", "path": "graphora/embedded.py", "line": 88, "calls": "records_for", "certain": true},
  {"name": "GraphStore.write_files", "path": "graphora/store.py", "line": 78, "calls": "records_for", "certain": true}
 ],
 "tests": 5
}
```

The server's instructions tell agents to call `what_could_break` before editing a symbol and
to report uncertain callers as uncertain.

---

## Use case 5: a CI gate in a few lines of library code <a name="use-case-5"></a>

```python
from graphora import index_repository, open_store
from graphora.impact import what_could_break_for_diff

store = open_store("myrepo", backend="embedded")
index_repository(".", project="myrepo", store=store)

impact = what_could_break_for_diff(store, pr_diff)
if impact.will_break and not impact.tests:
    raise SystemExit("This change certainly breaks callers and no test reaches it:\n" + impact.to_text())
```

Incremental re-indexing for watch mode or CI: `update_files(root, ["changed.py"], store=store)`
re-parses only those files and relinks the whole graph, so callers in unchanged files keep
their edges.

---

## Use case 6: agent session memory as a graph

**Scenario**: you work with AI coding agents in many terminal tabs at once. Each tab has
its own context, and after a day you no longer remember which window did what, which
sessions touched the same file, or which ones relate to the PR you're reviewing.

The Copilot CLI already records every session in a local SQLite store
(`~/.copilot/session-store.db`): summaries, messages, files touched, PR/issue/commit
references. Graphora ingests it as just another data source — no code parsing, same
graph, same tooling:

```bash
graphora sessions ingest --days 7
```

Captured output against a synthetic Copilot session store (the one the test suite builds;
real stores give the same shape, one entry per agent found):

```json
{
  "project": "agent-sessions",
  "graph": "graphora:agent-sessions",
  "sources": {
    "copilot": {
      "sessions": 3,
      "files_touched": 3,
      "refs": 2
    }
  }
}
```

The graph is `(:Session)-[:TOUCHED]->(:WorkFile)`, `(:Session)-[:IN_REPO]->(:Repo)`,
`(:Session)-[:REFERENCES]->(:Ref)`. Sessions from different terminals become connected
the moment they touch the same file, repo, or PR. Then ask relationship questions that
are painful in SQL and trivial in Cypher:

```bash
graphora sessions connected file build.yml    # which windows touched this file?
graphora sessions connected ref 275           # which sessions relate to PR 275?
graphora sessions connected repo org/proj     # everything that happened in one repo
```

**How to use it** (step by step):

1. `pip install graphora-kg`
2. Have FalkorDB running (`docker run -d -p 6379:6379 falkordb/falkordb`), **or** skip
   Docker entirely and add `--backend embedded` to every command below.
3. `graphora sessions ingest --days 7` — ingests every agent store found on the machine
   (Copilot CLI, Claude Code, Codex CLI), skips missing ones, safe to re-run anytime.
   Use `--source copilot|claude|codex` to ingest just one.
4. Ask away: `graphora sessions connected file <name>` / `ref <pr-number>` / `repo <name>`.
   Each hit is tagged with the agent that did the work — cross-agent memory.
5. Zero-effort mode: `graphora install-skill all --skill sessions` teaches your agents
   (22 supported) to run these commands themselves when you ask about past work.
6. Optional: `graphora serve-mcp --project agent-sessions` exposes your work history to
   any MCP-capable agent.

Captured output (`graphora sessions connected file build.yml`, same synthetic store):

```json
[
  {
    "session": "aaa11111",
    "agent": "copilot",
    "summary": "Fix build",
    "updated_at": "2026-10-16 22:43:47",
    "via": "/home/u/proj/build.yml"
  },
  {
    "session": "bbb22222",
    "agent": "copilot",
    "summary": "Review PR",
    "updated_at": "2026-10-16 22:43:47",
    "via": "/home/u/proj/build.yml"
  }
]
```

**Why it matters**: tools like graphify map your *code*; Graphora also maps your *work*.
Ingestion is deterministic and read-only on the source (no LLM, no writes to the session
store), idempotent (re-ingesting never duplicates nodes), and reuses the live FalkorDB
graph, so the MCP server exposes your work history to any agent for free.

---

## How it was tested <a name="how-it-was-tested"></a>

272 tests (`python -m pytest tests -q`), run in CI against a live FalkorDB on mcp 1.x and 2.x;
the job fails if any test is skipped.

| Area | What the tests pin down |
| --- | --- |
| Definitions (`test_core_coverage.py`) | For each of the 11 languages, the *exact* set of (kind, qualified name) the parser must return, so a missed definition or a false one (a `struct` usage, an interface signature) fails. Python is also checked against `ast`. |
| Resolution (`test_core_resolve.py`, `test_core_resolve_languages.py`) | One test per rule, in each language (Java fields and implicit `this`, Rust `use`/`impl`, C includes, C++ namespaces, Ruby `require_relative`, PHP `use`, TS typed fields, JS object literals, Go typed variables): imports vs same-name functions, `src/` layouts, relative and submodule imports, external imports (no edge), `self`, constructors, re-exports, Go packages, JS default/named imports, variable and return types, unions, the candidate cap, test doubles. |
| What could break (`test_core_impact.py`, `test_core_review.py`) | Certain vs uncertain, transitive depth and chains, uncertainty propagating up a path, classes including their methods, diffs mapped to enclosing symbols (body edits, deleted functions, new and deleted files), the "X or Y" text, JSON output, identical answers on both backends, review with and without an LLM, the CLI from `--git-range` and stdin. |
| Stores | Both backends end to end, a parity test (embedded == FalkorDB), incremental updates keeping cross-file callers. |
| Robustness | Deeply nested expressions, a file whose parser throws, form feeds and Unicode line separators, lone surrogates, linear-time parsing of a 20,000-function file, star-import cycles, `.gitignore` and generated files, a mistyped `index` path (the graph is kept). |
| Diffs (`test_core_diff.py`) | GNU and git-quoted headers, header-like lines inside hunks, deletions at a function's end, deleted methods, callers of deleted functions (certain and uncertain). |
| CLI, MCP, skills, sessions, benchmarks | Every command via `main()`, every MCP tool, skill installation for all agents, session ingestion, deterministic benchmarks. |
