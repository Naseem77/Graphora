from __future__ import annotations

import asyncio

import litellm

from app.ci.graph_context import GraphEvidence, build_ci_graph_evidence
from app.ci.logs import extract_failed_tests, extract_paths, fetch_workflow_run_logs, summarize_failure_logs
from app.config import Settings, get_settings
from app.github.app import get_github_client
from app.github.poster import post_pr_review_comment
from app.state import ci_debug_already_processed, mark_ci_debug_posted


CI_DEBUG_HEADER = "## Graphora CI Debug"
FAILURE_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}


async def debug_workflow_run(payload: dict, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    if payload.get("action") != "completed":
        return None

    workflow_run = payload.get("workflow_run") or {}
    if workflow_run.get("conclusion") not in FAILURE_CONCLUSIONS:
        return None

    pull_requests = workflow_run.get("pull_requests") or []
    if not pull_requests:
        return None

    repository = payload["repository"]
    owner = repository["owner"]["login"]
    repo = repository["name"]
    installation_id = payload["installation"]["id"]
    pr_number = int(pull_requests[0]["number"])
    workflow_run_id = int(workflow_run["id"])
    head_sha = str(workflow_run.get("head_sha") or "")

    if ci_debug_already_processed(owner, repo, workflow_run_id, head_sha, settings):
        return None

    github_client = await asyncio.to_thread(get_github_client, installation_id, settings)
    repo_obj = await asyncio.to_thread(github_client.get_repo, f"{owner}/{repo}")
    pr = await asyncio.to_thread(repo_obj.get_pull, pr_number)
    changed_files = await asyncio.to_thread(_pull_request_changed_file_names, pr)
    logs = await asyncio.to_thread(fetch_workflow_run_logs, installation_id, workflow_run.get("logs_url", ""), settings)
    failed_tests = extract_failed_tests(logs)
    log_paths = extract_paths(logs)
    log_summary = summarize_failure_logs(logs)
    evidence = await asyncio.to_thread(
        build_ci_graph_evidence,
        owner,
        repo,
        pr_number,
        changed_files,
        failed_tests,
        log_paths,
        settings,
    )
    debug_text = await asyncio.to_thread(_ask_llm_for_ci_debug, workflow_run, log_summary, evidence, settings)
    body = f"{CI_DEBUG_HEADER}\n\n{debug_text}"
    await asyncio.to_thread(post_pr_review_comment, github_client, owner, repo, pr_number, body)
    mark_ci_debug_posted(owner, repo, pr_number, workflow_run_id, head_sha, settings)
    return debug_text


def _ask_llm_for_ci_debug(workflow_run: dict, log_summary: str, evidence: GraphEvidence, settings: Settings) -> str:
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
        raise RuntimeError(f"Missing required Azure CI debug settings: {', '.join(missing)}")

    prompt = f"""You are Graphora's CI debugging agent. Find the most likely root cause of this failed CI run.

Use the graph evidence as the primary signal. Use logs to identify the failing test/error. Do not invent certainty.

Workflow: {workflow_run.get("name", "unknown")}
Conclusion: {workflow_run.get("conclusion", "unknown")}
Head SHA: {workflow_run.get("head_sha", "unknown")}

## Failure log summary
{log_summary}

## Repository graph evidence
{evidence.to_prompt_context()}

Write a concise pull request comment with:
1. Failed test or failing job if known.
2. Likely cause, using dependency paths when available.
3. Suggested files to inspect.
4. Next debugging step.
5. Confidence: high/medium/low.

If graph evidence is missing, say that and fall back to log-based reasoning.
"""
    response = litellm.completion(
        model=settings.review_model,
        api_key=settings.azure_api_key,
        api_base=settings.azure_api_base,
        api_version=settings.azure_api_version,
        max_tokens=2048,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return str(response.choices[0].message.content or "")


def _pull_request_changed_file_names(pr: object) -> list[str]:
    return [file.filename for file in pr.get_files() if getattr(file, "status", "") != "removed"]
