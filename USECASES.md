# Graphora Use Cases

Five real scenarios, all executed locally and captured verbatim on 2026-07-14.
Environment: macOS, Python 3.11, FalkorDB in Docker on `localhost:6379`.

Setup used for all of them:

```bash
docker run -d --name graphora-falkordb -p 6379:6379 falkordb/falkordb:latest
pip install graphora-kg
graphora index . --project graphora          # 17 files, 148 functions, 251 call edges
graphora risk mine . --project graphora
```

---

## Use case 1: cross-file impact review with zero LLM <a name="use-case-1"></a>

**Scenario**: a one-line diff adds a `decay` parameter to `compute_risk_score()`.
A diff-only reviewer sees one file and has to guess the impact.

```bash
graphora review --diff change.diff --project graphora
```

Captured output:

```
# Graphora Review

`compute_risk_score` has 1 caller(s) and 1 covering test(s).

## Grounded facts (from the graph)

Blast radius (from code graph, deterministic):

### compute_risk_score (Function)
- defined at graphora/risk.py:130 `def compute_risk_score(fix_count: int, last_broke_at: str, now: datetime | None = None) -> float:`
- callers (1): _recompute_risk_scores [graphora/risk.py] (EXTRACTED)
- calls (0): none
- tests (1): test_risk_score_decays_over_time [tests/core/test_core_risk.py] (INFERRED)
- imported by (3): graphora/cli.py, graphora/mcp_server.py, tests/core/test_core_risk.py

_Context sent to model: ~197 tokens (graph blast radius, not the repo)._
```

**Why it matters**: ~197 tokens of context instead of a ~22,200-token repo dump, no LLM
call at all, and every conclusion (one known caller, one covering test, three importing
files) is read from the graph, not guessed. Add `--llm` with `REVIEW_MODEL` set and
the same tiny context feeds a full model-written review.

---

## Use case 2: risk memory finds where a real codebase breaks

**Scenario**: point Graphora at falkordb-py, a real client library with 231 commits of
history, and ask where it historically breaks.

```bash
cd ../falkordb-py
graphora index . --project falkordb-py
graphora risk mine . --project falkordb-py     # scanned history, found 40 fix/revert commits
graphora risk top --project falkordb-py
```

Captured output:

```
  RISK  FIXES  CALLERS  LAST BROKE   SYMBOL
  0.14      3        0  2025-05-28   test_graph_creation  (tests/test_async_graph.py:10)
  0.08      2        2  2025-03-13   Is_Cluster  (falkordb/asyncio/cluster.py:9)
  0.07      1        0  2025-06-19   QueryResult  (falkordb/query_result.py:355)
  0.05      1        0  2025-03-12   FalkorDB  (falkordb/asyncio/falkordb.py:10)
  0.05      1        2  2025-03-12   Cluster_Conn  (falkordb/asyncio/cluster.py:26)
```

**Why it matters**: the async cluster code surfaces as the genuine historical trouble
spot, derived purely from commit metadata and diffs, at $0. From now on, any review or
blast radius touching `Is_Cluster` automatically carries a risk-memory finding:

> **risk-memory**: `Is_Cluster` was involved in 2 past fix/revert commits (last: 2025-03-13), risk score 0.08, 2 callers. Review extra carefully.

Mining is idempotent per commit, so re-running never double-counts. The longer it runs
on a repository, the smarter it gets. No stateless reviewer can offer this.

---

## Use case 3: any AI agent, via MCP

**Scenario**: give Copilot CLI, Claude Code, Cursor, or any MCP client structured access
to the graph, so agents check impact *before* editing.

```bash
graphora serve-mcp --project falkordb-py
```

A real captured stdio session (MCP Python client):

```
TOOLS: ['graph_stats', 'blast_radius', 'review_diff', 'risk_top', 'find_symbol']

blast_radius(query): confidence-tagged JSON of callers/callees/tests/risk

risk_top(3):
  0.14 test_graph_creation (tests/test_async_graph.py) fixes=3
  0.08 Is_Cluster (falkordb/asyncio/cluster.py) fixes=2, callers=2
  0.07 QueryResult (falkordb/query_result.py) fixes=1

find_symbol(Is_Cluster): falkordb/asyncio/cluster.py:9 `def Is_Cluster(conn: redis.Redis):`
```

**Why it matters**: the server's instructions tell agents to call `blast_radius` before
touching a symbol. That turns Graphora into a guardrail for AI-generated changes: the
agent learns "this function has 5 callers and broke twice" *before* it edits, not after
CI fails.

---

## Use case 4: a CI gate in three lines of library code

**Scenario**: block PRs that change symbols which have callers but no covering tests.
Executed live against the Graphora graph:

```python
from graphora import GraphStore
from graphora.blast import blast_radius_for_diff

radius = blast_radius_for_diff(GraphStore("myrepo"), pr_diff)
untested = [s.name for s in radius.symbols if s.callers and not s.tests]
assert not untested, f"Changed symbols with callers but no tests: {untested}"
```

The same library API drove two more workflows in the same session:

```python
# Refactor planning: all call sites of a function, confidence-tagged
blast_radius(store, ["parse_code_file"])   # returned every call site

# Incremental re-index after editing one file (no full rebuild)
update_files(repo_root, ["graphora/benchmark.py"], store=store)
```

**Why it matters**: no webhook, no bot, no server. Import and query. This is the
"tool usable in code" mode: CI gates, pre-refactor checks, custom dashboards, editor
plugins all build on the same three functions.

---

## Use case 5: confidence tags catch real ambiguity

**Scenario**: while querying its own graph, Graphora hit a genuinely ambiguous case, and
told the truth about it.

The repository defines *two* functions named `blast_radius` (the core implementation in
`graphora/blast.py` and the MCP tool wrapper in `graphora/mcp_server.py`). A name-based
caller match cannot know which one a call refers to. Captured output:

```
blast_radius @ graphora/blast.py:90       -> callers: run_benchmark(AMBIGUOUS), cmd_blast(AMBIGUOUS), blast_radius_for_diff(EXTRACTED)
blast_radius @ graphora/mcp_server.py:41  -> callers: run_benchmark(AMBIGUOUS), cmd_blast(AMBIGUOUS)
```

**Why it matters**: instead of silently picking one candidate (and being wrong half the
time), every candidate is linked and tagged `AMBIGUOUS`. Same-file calls are `EXTRACTED`,
single-candidate cross-file resolutions are `INFERRED`. A reviewer, an agent, or an LLM
downstream can weigh facts accordingly. This honesty is what makes graph output
trustworthy enough to gate merges on.

---

## How it was tested <a name="how-it-was-tested"></a>

Test-first at every phase; the full suite had to pass before the next phase started.

| Phase | Tests | What they verify |
| --- | ---: | --- |
| 1. Parser + store | 13 | Symbol/import/call extraction per language, confidence rules (same-file call = EXTRACTED, cross-file = INFERRED), test-file detection, ignore lists, incremental update/delete against live FalkorDB |
| 2. Blast radius | 6 | Callers and tests found, test callers excluded, confidence propagation, unresolved symbols, diff-driven radius, compact context |
| 3. Risk memory | 7 | Fix/hotfix/revert detection on a scripted real git repo, feature commits excluded, idempotent re-runs, revert kind, ranking, risk decay math, blast integration |
| 4. CLI + review | 6 | Every command end-to-end via `main()`, JSON output, facts-mode review, risk warnings surfaced |
| 5. MCP server | 4 | All 5 tools listed and invoked on the real server object, JSON payloads validated |
| 6. Benchmark | 5 | Savings hold on a realistic fixture, grounded-fact counts, determinism (two runs byte-identical), report rendering |
| 7. Embedded backend | 12 | JSON persistence across instances, blast/risk/CLI end-to-end without a server, auto-fallback, byte-identical output parity with FalkorDB |
| 8. Skill installer | 11 | 22 agents supported, frontmatter formats, idempotent marked blocks, existing files preserved, CLI end-to-end |
| 9. Languages | 14 | Rust/C/C++/Ruby/PHP symbols, imports, calls via tree-sitter; qualified-name resolution; 4-language end-to-end index; diff attribution |
| 10. Public benchmark | 3 | Deterministic rows, markdown rendering, all repos pinned to full 40-char SHAs |

Final state:

```
$ python3 -m pytest tests -q
80 passed in 3.4s
```

All 80 core tests, green. Integration tests run against a live FalkorDB and real
per-test git repositories, and skip
cleanly when FalkorDB is not running.

Two bugs were caught by the tests themselves during development and fixed on the spot:

1. **git `--grep` portability**: `\b` word boundaries are not supported by git's ERE on
   macOS, so fix-commit filtering silently matched nothing. Fix: list commits and filter
   in Python with a proper regex.
2. **Unfair benchmark baseline**: "changed files only" was informationally weaker than
   the blast radius (it cannot tell you callers or tests). Fix: the baseline now includes
   every file containing a caller or covering test, which is the honest comparison.
