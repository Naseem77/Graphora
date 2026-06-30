# GraphReview Bot

GraphReview Bot is a GitHub App that builds FalkorDB knowledge graphs for repositories and uses GraphRAG context to review pull requests. It indexes the main branch, creates temporary PR overlay graphs, posts review summaries, and answers `@graphreview` questions in PR comments.

## What it does

| Event | Behavior |
| --- | --- |
| GitHub App installed | Queues a main graph build for the installed repository |
| Push to main | Queues changed-file updates in the main graph |
| PR opened or updated | Queues PR overlay refresh, computes main-vs-PR diff context, and posts one CodeRabbit-style review (walkthrough + inline comments) per head SHA |
| GitHub Actions workflow failed | Uses CI logs plus the PR graph to explain the likely root cause and related files |
| PR closed or merged | Deletes the temporary PR graph |
| `@graphreview` comment | Runs commands (`review`, `summary`, `pause`, `resume`, `help`) or answers questions using graph context |

## Architecture

```text
GitHub App webhooks
  -> FastAPI backend
  -> GitHub API
  -> code parser
  -> FalkorDB graphs
  -> GraphRAG-SDK retrieval
  -> configurable AI provider
  -> GitHub PR comments
```

## Graph model

GraphReview stores structured code metadata in FalkorDB.

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

**Important properties**

```text
id
owner
repo
graph_scope
path
name
kind
line
language
signature
stable_key
content_hash
signature_hash
```

These properties allow main-vs-PR comparison by stable symbol identity and hashes, not only by line number.

**Relationship types**

| Relationship | Purpose |
| --- | --- |
| `DEFINED_IN` | Connects symbols to their source file |
| `IMPORTS` | Connects source files to imported modules |
| `CALLS` | Connects caller functions to callee functions when both are present |
| `DEPENDS_ON` | Connects source files to imported modules for dependency traversal |
| `DOCUMENTS` | Connects documentation pages to their backing file |
| `HAS_SECTION` | Connects documentation pages to heading sections |

GraphReview stores operational state in FalkorDB under the dedicated `graph:graphreview:state` graph. This keeps webhook delivery idempotency, graph build status, and PR review idempotency in the same backend as the knowledge graph while keeping it separate from repository graphs.

## Requirements

- Docker and Docker Compose
- GitHub App private key
- AI provider credentials
- Public HTTPS URL for GitHub webhooks, such as ngrok or a production domain

## Environment

Create `.env` from the example:

```bash
cp .env.example .env
```

Minimum required values:

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

`REVIEW_MODEL` is the provider-specific model or deployment name. The current example uses Azure-compatible LiteLLM format.

## GitHub App setup

Create a GitHub App from:

```text
GitHub -> Settings -> Developer settings -> GitHub Apps -> New GitHub App
```

Recommended settings:

| Setting | Value |
| --- | --- |
| Homepage URL | Repository URL or project website |
| Callback URL | Leave empty |
| Webhook URL | `https://your-domain.com/webhook` |
| Webhook secret | Same value as `GITHUB_WEBHOOK_SECRET` |
| Installation target | Only on this account, unless deploying broadly |

Repository permissions:

| Permission | Access |
| --- | --- |
| Contents | Read-only |
| Issues | Read and write |
| Metadata | Read-only |
| Pull requests | Read and write |
| Actions | Read-only |

Subscribe to events:

```text
Installation
Push
Pull request
Workflow run
Issue comment
Pull request review comment
```

After creating the app:

1. Copy the App ID into `GITHUB_APP_ID`.
2. Generate a private key.
3. Save the `.pem` file locally.
4. Set `GITHUB_PRIVATE_KEY_PATH` to that file path.
5. Install the app on the target repository.

## Running locally

Start the stack:

```bash
docker compose up --build
```

Check health:

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status":"ok"}
```

Expose the local app to GitHub:

```bash
ngrok http 8000
```

Use the ngrok HTTPS URL with `/webhook` as the GitHub App webhook URL:

```text
https://your-ngrok-domain.ngrok-free.app/webhook
```

## Using the bot

Open or update a pull request. GraphReview posts a `GraphReview Analysis` comment with a summary, a per-file walkthrough table, and line-level inline review comments (CodeRabbit-style), all grounded in the knowledge graph.

If GitHub Actions fails on a pull request, GraphReview posts a `Graphora CI Debug` comment. The CI debugging agent combines failure logs with the repository graph to identify likely dependency paths from the failed test to the changed files.

Commands and questions in a PR comment:

```text
@graphreview review                         # re-run a full review now
@graphreview summary                        # post a summary/walkthrough
@graphreview pause                          # stop automatic reviews on new commits
@graphreview resume                         # re-enable automatic reviews
@graphreview help                           # list commands
@graphreview what changed in this PR?       # free-form graph question
@graphreview what could this break?
```

## Multi-tenant (per-installation FalkorDB)

By default all installations share one FalkorDB. Set `GRAPHORA_PER_INSTALL_DB=true` to give each GitHub App installation its own FalkorDB instance, provisioned on install and torn down on uninstall. Provisioning sits behind the `GraphProvisioner` interface (`app/provision/`); the included implementation uses the local Docker daemon (mount `/var/run/docker.sock` into the app), and can be swapped for Kubernetes or a cloud orchestrator without touching the rest of the app. A registry in the shared control-plane FalkorDB maps each `installation_id` to its instance host/port.

## Viewing graphs

Open FalkorDB Browser:

```text
http://localhost:3000
```

Example queries:

```cypher
MATCH (n) RETURN n LIMIT 50
```

```cypher
MATCH (f:File) RETURN f.path, f.content_hash, f.symbol_count LIMIT 20
```

```cypher
MATCH (fn:Function) RETURN fn.stable_key, fn.signature_hash LIMIT 20
```

List graph names from the terminal:

```bash
docker compose exec falkordb redis-cli GRAPH.LIST
```

## Backup

FalkorDB data is stored in the `falkordb_data` Docker volume. To snapshot and upload to S3 or Cloudflare R2:

```bash
scripts/backup_falkordb.sh
```

Required backup environment variables:

```env
S3_BUCKET=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

## Tests

Run the local test suite:

```bash
pytest
```

## Security notes

- Do not commit `.env` files.
- Do not commit GitHub App private keys.
- Rotate any token or key that is accidentally shared.
- Use HTTPS for production webhook delivery.
- Install the GitHub App only on repositories it needs to access.
