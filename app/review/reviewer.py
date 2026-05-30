from __future__ import annotations

import asyncio

import litellm

from app.config import Settings, get_settings
from app.graph.pr_context import build_pr_context
from app.github.poster import post_pr_review_comment
from app.parser.treesitter import extract_symbols_from_diff


REVIEW_HEADER = "## GraphReview Analysis"


async def review_pr(owner: str, repo: str, pr_number: int, diff: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    context = await asyncio.to_thread(build_pr_context, owner, repo, diff, pr_number)
    return await asyncio.to_thread(_ask_llm_for_review, diff, context, settings)


async def review_and_post_pr(
    github_client: object,
    owner: str,
    repo: str,
    pr_number: int,
    diff: str,
    settings: Settings | None = None,
) -> str:
    review_text = await review_pr(owner, repo, pr_number, diff, settings)
    post_pr_review_comment(github_client, owner, repo, pr_number, f"{REVIEW_HEADER}\n\n{review_text}")
    return review_text


def _ask_llm_for_review(diff: str, context: str, settings: Settings) -> str:
    missing = [
        name
        for name, value in (
            ("AZURE_API_KEY", settings.azure_api_key),
            ("AZURE_API_BASE", settings.azure_api_base),
            ("REVIEW_MODEL", settings.review_model),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required Azure review settings: {', '.join(missing)}")

    changed_symbols = extract_symbols_from_diff(diff)
    symbol_text = ", ".join(changed_symbols) if changed_symbols else "No top-level symbols detected"
    prompt = f"""You are a senior code reviewer. Review the following PR diff using the knowledge graph context.

Changed symbols: {symbol_text}

## PR Diff
{diff}

## Codebase Context
{context}

Review only meaningful issues:
- Bugs and logic errors
- Breaking changes in callers/dependencies
- Missing test coverage
- Security vulnerabilities
- Performance regressions

For each issue include file path, line number if available, severity (critical/warning/suggestion), explanation, and a suggested fix.
If there are no meaningful issues, say that clearly and mention the most relevant context checked.
"""

    response = litellm.completion(
        model=settings.review_model,
        api_key=settings.azure_api_key,
        api_base=settings.azure_api_base,
        api_version=settings.azure_api_version,
        max_tokens=4096,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return str(response.choices[0].message.content or "")
