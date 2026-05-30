# GraphReview Bot

GraphReview Bot is a GitHub App backend that builds FalkorDB knowledge graphs for repositories, uses GraphRAG-SDK retrieval for codebase context, and posts Azure OpenAI-powered pull request reviews. Developers can also ask `@graphreview` questions in PR comments.

## Local setup

1. Copy `.env.example` to `.env` and fill in the GitHub App, Azure OpenAI, FalkorDB, and optional S3 settings.
2. Save the GitHub App private key as `private-key.pem` in the project root.
3. Run the stack:

```bash
docker compose up --build
```

The API is available at `http://localhost:8000`, health checks at `/health`, and GitHub webhooks at `/webhook`. FalkorDB Browser is exposed on `http://localhost:3000`.

Azure OpenAI settings use LiteLLM format:

```env
AZURE_API_KEY=your_azure_key
AZURE_API_BASE=https://your-resource.openai.azure.com
AZURE_API_VERSION=2024-02-15-preview
REVIEW_MODEL=azure/your-deployment-name
```

## GitHub integration

Create a GitHub App in **GitHub Settings -> Developer settings -> GitHub Apps -> New GitHub App**.

- **Webhook URL:** `https://your-domain.com/webhook`
- **Webhook secret:** same value as `GITHUB_WEBHOOK_SECRET`
- **Permissions:** Pull requests read/write, Contents read, Issues read/write, Metadata read
- **Events:** Installation, Push, Pull request, Issue comment, Pull request review comment

Install the app on the target repositories. On installation the bot indexes code into `graph:{owner}:{repo}:main`; on pushes it updates changed files; on PR open/update it posts a GraphReview analysis; and in PR comments it answers questions that mention `@graphreview`.

## Backup

Run `scripts/backup_falkordb.sh` on a cron schedule from a container or host that can access FalkorDB and AWS/R2 credentials. It snapshots FalkorDB and uploads `dump.rdb` to `s3://$S3_BUCKET/falkordb/`.

## Tests

```bash
pytest
```
