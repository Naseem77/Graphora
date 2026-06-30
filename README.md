<div align="center">

# 🕸️ Graphora — GraphReview Bot

### Graph-grounded AI code review for GitHub. Cheaper, sharper, trustworthy.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![FalkorDB](https://img.shields.io/badge/FalkorDB-graph-FF4438?logo=redis&logoColor=white)](https://www.falkordb.com/)
[![tree--sitter](https://img.shields.io/badge/tree--sitter-parsing-2C2C2C)](https://tree-sitter.github.io/tree-sitter/)
[![GitHub App](https://img.shields.io/badge/GitHub-App-181717?logo=github&logoColor=white)](https://docs.github.com/en/apps)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](#license)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#-contributing)

`#ai-code-review` · `#knowledge-graph` · `#falkordb` · `#tree-sitter` · `#github-app` · `#llm` · `#token-efficiency`

</div>

---

> **TL;DR** — Most AI reviewers see only the diff, so they miss cross-file impact and overpay for context. Graphora keeps a **live structural graph** of your repo in FalkorDB and feeds the model just the **blast radius** of a change (callers, callees, tests). Result on a real PR: **~99% fewer prompt tokens** and review claims grounded in actual code structure.

## 📑 Table of contents

- [Why Graphora](#-why-graphora)
- [How it works](#-how-it-works)
- [Use case: cross-file impact](#-use-case-cross-file-impact)
- [Benchmark: token cost](#-benchmark-token-cost)
- [Quick start](#-quick-start)
- [Using the bot](#-using-the-bot)
- [Reference](#-reference) *(events, graph model, config, multi-tenant, ops)*
- [Security](#-security)

---

## 🎯 Why Graphora

AI code review today is mostly *"diff in, comment out."* That creates three problems:

| ❌ Problem | What goes wrong |
| --- | --- |
| **Blind to blast radius** | A one-line change to a shared function looks harmless in isolation. The reviewer can't see the callers, callees, and tests it affects — they live in other files outside the diff. |
| **Expensive to compensate** | The usual fix is to stuff more of the repo into the prompt. That burns tokens fast and is still imprecise — most of the context is irrelevant to the change. |
| **Hard to trust** | Without grounding in real structure, the model guesses (*"this is probably safe"*), and reviewers can't tell a grounded claim from a hallucination. |

**Graphora fixes the root cause: it gives the model structure, not more text.**

| ✅ With Graphora | Benefit |
| --- | --- |
| Live structural graph of the repo | Knows the real call/dependency relationships, across files |
| Sends only a change's blast radius | Precise context → lower cost, higher signal |
| Claims tie back to graph facts | Grounded, auditable, trustworthy review |

---

## ⚙️ How it works

Graphora builds a **deterministic structural graph** with tree-sitter (no LLM in the indexing path) and stores it in FalkorDB: `File`, `Function`, `Class`, `Module` nodes connected by `CALLS`, `DEFINED_IN`, `IMPORTS`, `DEPENDS_ON` edges.

```text
  GitHub webhook ─▶ FastAPI ─▶ tree-sitter parse ─▶ FalkorDB graph
                                                          │
        PR opened ──▶ extract changed symbols from diff   │
                          │                                │
                          ▼                                ▼
                  query blast radius  ◀── callers / callees / tests
                          │
                          ▼
            small precise context ─▶ AI model ─▶ grounded PR review
```

On every PR, Graphora:
1. **Extracts** the changed symbols from the diff.
2. **Queries** the graph for each symbol's **definitions, callers, callees, and tests**.
3. **Feeds** the model the diff + that impact — not the whole repo.
4. **Posts** a CodeRabbit-style review (walkthrough + inline findings), grounded in the graph.

It also maintains a temporary **PR overlay graph** per pull request (`graph:{owner}:{repo}:pr:{n}`), deleted on merge/close, so PR analysis never pollutes the main graph.

---

## 🔬 Use case: cross-file impact

**PR:** add a backward-compatible `force_refresh` parameter to `get_provisioner()` — a *one-file* diff.

A diff-only reviewer sees one file. Graphora queries the **live graph** and finds the real blast radius:

```text
### get_provisioner
- defined at app/provision/factory.py:16
- callers (3): settings_for_installation, provision_installation, destroy_installation
- calls (0): none
- tests (0): none
```

Grounded in that, the bot concluded the change is **backward-compatible** — it *knew* the 3 callers don't pass the new argument — and flagged that the new behavior has **no test coverage** (`tests (0)`). Two claims a diff-only reviewer cannot make with confidence.

> 🧠 The reviewer didn't guess the impact. It *read it from the graph.*

---

## 📊 Benchmark: token cost

Context sent to the model for the use-case PR above:

| Approach | Context to the LLM | Relative |
| --- | --- | --- |
| Dump the whole repo | **~29,200 tokens** | 100% |
| **Graphora** (diff + blast radius) | **~430 tokens** | **~1%** |

```text
Whole-repo dump  ██████████████████████████████████████████████████  ~29,200 tokens
Graphora         ▏                                                    ~430 tokens   (~99% less)
```

**~99% fewer prompt tokens — and a *more* accurate review**, because the graph sends the symbols that matter instead of everything. The advantage compounds on large codebases: the graph still isolates the handful of real callers among thousands of files, which neither a raw diff nor a context window can do well.

---

## 🚀 Quick start

> **Prerequisites:** Docker + Docker Compose, a GitHub App (private key), AI provider credentials, and a public HTTPS URL (e.g. ngrok).

```bash
# 1. Configure
cp .env.example .env        # fill in GitHub App + AI provider values

# 2. Run the stack (FastAPI + FalkorDB)
docker compose up --build

# 3. Verify
curl http://localhost:8000/health        # -> {"status":"ok"}

# 4. Expose to GitHub
ngrok http 8000                          # use https://<id>.ngrok-free.app/webhook
```

Point your GitHub App's **Webhook URL** at `https://<your-domain>/webhook`, install it on a repo, and open a PR. Graphora posts a **GraphReview Analysis** comment grounded in the graph.

📺 Live status dashboard: **`http://localhost:8000/dashboard`** (auto-refreshing build status + per-PR graph stats and logs).

<details>
<summary><b>Minimum <code>.env</code> values</b></summary>

```env
GITHUB_APP_ID=
GITHUB_PRIVATE_KEY_PATH=./private-key.pem
GITHUB_WEBHOOK_SECRET=

AZURE_API_KEY=
AZURE_API_BASE=https://your-resource.openai.azure.com
AZURE_API_VERSION=2024-02-15-preview
REVIEW_MODEL=azure/your-deployment-name

FALKORDB_HOST=falkordb
FALKORDB_PORT=6379
GRAPHREVIEW_BOT_LOGIN=graphreview
```

`REVIEW_MODEL` is the provider-specific model/deployment name (LiteLLM format; the example uses Azure).
</details>

---

## 💬 Using the bot

Open or update a PR — Graphora posts a `GraphReview Analysis` with a summary, per-file walkthrough, and line-level inline comments, all grounded in the graph. If GitHub Actions fails on the PR, it posts a `Graphora CI Debug` comment combining failure logs with the graph to trace likely root-cause paths.

Commands and questions in a PR comment:

```text
@graphreview review                    # re-run a full review now
@graphreview summary                   # post a summary / walkthrough
@graphreview pause                     # stop automatic reviews on new commits
@graphreview resume                    # re-enable automatic reviews
@graphreview help                      # list commands
@graphreview what changed in this PR?  # free-form, graph-grounded question
@graphreview what could this break?
```

---

## 📚 Reference

<details>
<summary><b>Event behavior</b></summary>

| Event | Behavior |
| --- | --- |
| GitHub App installed | Queues a main graph build for the installed repository |
| Push to main | Queues changed-file updates in the main graph |
| PR opened or updated | Refreshes the PR overlay, computes main-vs-PR diff context, and posts one review per head SHA |
| GitHub Actions workflow failed | Uses CI logs + the PR graph to explain the likely root cause and related files |
| PR closed or merged | Deletes the temporary PR graph |
| `@graphreview` comment | Runs commands or answers questions using graph context |
</details>

<details>
<summary><b>Graph model</b></summary>

**Graph names**

```text
graph:{owner}:{repo}:main
graph:{owner}:{repo}:pr:{number}
```

**Node types**

| Node | Purpose |
| --- | --- |
| `File` | Source file metadata |
| `Function` | Function or method symbol |
| `Class` | Class, struct, or type symbol |
| `Module` | Imported module/package |
| `DocPage` | Markdown or MDX documentation file |
| `DocSection` | Heading inside a documentation file |

**Relationship types**

| Relationship | Purpose |
| --- | --- |
| `DEFINED_IN` | Connects symbols to their source file |
| `IMPORTS` | Connects source files to imported modules |
| `CALLS` | Connects caller functions to callee functions |
| `DEPENDS_ON` | Connects source files to imported modules for dependency traversal |
| `DOCUMENTS` | Connects documentation pages to their backing file |
| `HAS_SECTION` | Connects documentation pages to heading sections |

**Key properties:** `id, owner, repo, graph_scope, path, name, kind, line, language, signature, stable_key, content_hash, signature_hash`.

These allow main-vs-PR comparison by stable symbol identity and hashes, not only by line number. Operational state (idempotency, build status) lives in a dedicated `graph:graphreview:state` graph, separate from repository graphs.
</details>

<details>
<summary><b>GitHub App setup</b></summary>

Create from **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**.

| Setting | Value |
| --- | --- |
| Webhook URL | `https://your-domain.com/webhook` |
| Webhook secret | Same as `GITHUB_WEBHOOK_SECRET` |
| Callback URL | Leave empty |

**Repository permissions:** Contents (read), Issues (read/write), Metadata (read), Pull requests (read/write), Actions (read).

**Subscribe to events:** Installation, Push, Pull request, Workflow run, Issue comment, Pull request review comment.

Then: copy the App ID → `GITHUB_APP_ID`; generate a private key, save the `.pem`, set `GITHUB_PRIVATE_KEY_PATH`; install the app on the target repo.
</details>

<details>
<summary><b>Multi-tenant (per-installation FalkorDB)</b></summary>

By default all installations share one FalkorDB. Set `GRAPHORA_PER_INSTALL_DB=true` to give each installation its own FalkorDB instance, provisioned on install and torn down on uninstall. Provisioning sits behind the `GraphProvisioner` interface (`app/provision/`); the included implementation uses the local Docker daemon (mount `/var/run/docker.sock`) and can be swapped for Kubernetes or a cloud orchestrator. A registry in the control-plane FalkorDB maps each `installation_id` to its instance host/port.
</details>

<details>
<summary><b>Viewing graphs & backup</b></summary>

Open **FalkorDB Browser** at `http://localhost:3000`:

```cypher
MATCH (caller:Function)-[:CALLS]->(f:Function {name:'get_provisioner'})
RETURN caller.name, caller.path
```

```cypher
MATCH (fn:Function) RETURN fn.stable_key, fn.signature_hash LIMIT 20
```

List graphs: `docker compose exec falkordb redis-cli GRAPH.LIST`

**Backup:** FalkorDB data lives in the `falkordb_data` Docker volume. Snapshot/upload with `scripts/backup_falkordb.sh` (needs `S3_BUCKET`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`).
</details>

<details>
<summary><b>Tests</b></summary>

```bash
pytest
```
</details>

---

## 🔐 Security

- Never commit `.env` files or GitHub App private keys.
- Rotate any token or key that is accidentally shared.
- Use HTTPS for production webhook delivery; webhook signatures are verified with a constant-time comparison.
- Install the GitHub App only on repositories it needs to access.

---

## 🤝 Contributing

Issues and PRs are welcome. Run `pytest` before submitting; keep changes focused and grounded — Graphora will review its own PRs.

## License

MIT.
