from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Awaitable, Callable

from fastapi import BackgroundTasks

from app.config import get_settings
from app.ci.debugger import debug_workflow_run
from app.github.app import get_github_client
from app.github.poster import post_or_update_pr_comment
from app.graph.builder import build_repo_graph, fetch_repository_files
from app.graph.pr_graph import build_pr_graph, delete_pr_graph
from app.graph.store import code_graph_has_files
from app.graph.updater import update_graph_for_push
from app.review.chat import answer_comment
from app.review.reviewer import review_and_post_pr
from app.state import mark_delivery, mark_review_posted, review_already_processed


logger = logging.getLogger(__name__)

WebhookHandler = Callable[[dict], Awaitable[None]]


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    secret = get_settings().github_webhook_secret
    if not secret:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is required to verify webhooks")
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def dispatch_webhook(
    event: str,
    payload: dict,
    background_tasks: BackgroundTasks | None = None,
    delivery_id: str | None = None,
) -> None:
    handlers: dict[str, WebhookHandler] = {
        "installation": handle_install,
        "push": handle_push,
        "pull_request": handle_pr,
        "issue_comment": handle_comment,
        "workflow_run": handle_workflow_run,
    }
    handler = handlers.get(event)
    if handler is None:
        logger.info("Ignoring unsupported GitHub event: %s", event)
        return

    if delivery_id and not mark_delivery(event, delivery_id):
        logger.info("Ignoring duplicate GitHub delivery for event=%s delivery_id=%s", event, delivery_id)
        return

    logger.info("Accepted GitHub event=%s repo=%s", event, _repository_name(payload))
    if background_tasks is not None:
        background_tasks.add_task(_run_handler_sync, handler, payload)
        return

    await handler(payload)


def _run_handler_sync(handler: WebhookHandler, payload: dict) -> None:
    asyncio.run(handler(payload))


async def handle_install(payload: dict) -> None:
    action = payload.get("action")
    if action not in {"created", "added"}:
        return

    installation_id = payload["installation"]["id"]
    github_client = get_github_client(installation_id)
    repositories = payload.get("repositories") or payload.get("repositories_added") or []
    for repository in repositories:
        owner = repository.get("owner", {}).get("login") or payload["installation"]["account"]["login"]
        repo = repository["name"]
        files = await asyncio.to_thread(fetch_repository_files, github_client, owner, repo)
        await asyncio.to_thread(build_repo_graph, owner, repo, files)


async def handle_push(payload: dict) -> None:
    installation_id = payload["installation"]["id"]
    repository = payload["repository"]
    owner = repository["owner"]["login"]
    repo = repository["name"]
    github_client = get_github_client(installation_id)
    repo_obj = github_client.get_repo(f"{owner}/{repo}")

    changed_paths = _changed_paths_from_commits(payload.get("commits", []))
    changed_files: list[dict[str, str]] = []
    ref = payload.get("after")
    from github.GithubException import GithubException

    for path in changed_paths:
        try:
            content_file = repo_obj.get_contents(path, ref=ref)
        except GithubException as exc:
            logger.info("Skipping deleted or unavailable file %s in %s/%s: %s", path, owner, repo, exc)
            continue
        changed_files.append(
            {
                "path": content_file.path,
                "content": content_file.decoded_content.decode("utf-8", errors="replace"),
            }
        )

    await asyncio.to_thread(update_graph_for_push, owner, repo, changed_files)


async def handle_pr(payload: dict) -> None:
    action = payload.get("action")
    if action == "closed":
        repository = payload["repository"]
        owner = repository["owner"]["login"]
        repo = repository["name"]
        pr_number = payload["pull_request"]["number"]
        await asyncio.to_thread(delete_pr_graph, owner, repo, pr_number)
        return

    if action not in {"opened", "synchronize", "reopened", "ready_for_review"}:
        return

    installation_id = payload["installation"]["id"]
    repository = payload["repository"]
    owner = repository["owner"]["login"]
    repo = repository["name"]
    pr_number = payload["pull_request"]["number"]
    head_sha = payload["pull_request"]["head"]["sha"]
    base_sha = payload["pull_request"]["base"]["sha"]

    if review_already_processed(owner, repo, pr_number, head_sha):
        logger.info("Skipping duplicate review for %s/%s PR #%s at %s", owner, repo, pr_number, head_sha)
        return

    github_client = get_github_client(installation_id)
    try:
        logger.info("Checking main graph for %s/%s before PR #%s", owner, repo, pr_number)
        main_progress = _LogProgressReporter(owner, repo, "Main graph")
        main_source_count = await _ensure_main_graph(github_client, owner, repo, base_sha, main_progress)
        if main_source_count is None:
            logger.info("Main graph already available for %s/%s", owner, repo)
        else:
            logger.info("Main graph loaded for %s/%s with %s files", owner, repo, main_source_count)
        await _post_pr_status_comment(github_client, owner, repo, pr_number, head_sha, "Building PR graph")
        repo_obj = github_client.get_repo(f"{owner}/{repo}")
        pr = repo_obj.get_pull(pr_number)
        changed_files = _pull_request_changed_files(repo_obj, pr)
        pr_progress = _PrProgressReporter(github_client, owner, repo, pr_number, head_sha, "PR graph")
        pr_source_count = await asyncio.to_thread(
            build_pr_graph,
            owner,
            repo,
            pr_number,
            changed_files,
            None,
            pr_progress,
        )
        await _post_pr_status_comment(
            github_client,
            owner,
            repo,
            pr_number,
            head_sha,
            f"PR graph ready with {pr_source_count} changed files; running review",
        )
        diff = _pull_request_diff(pr)
        await review_and_post_pr(github_client, owner, repo, pr_number, diff)
        mark_review_posted(owner, repo, pr_number, head_sha)
        await _post_pr_status_comment(github_client, owner, repo, pr_number, head_sha, "Review complete")
    except Exception as exc:
        logger.exception("PR analysis failed for %s/%s PR #%s at %s", owner, repo, pr_number, head_sha)
        await _post_pr_status_comment(
            github_client,
            owner,
            repo,
            pr_number,
            head_sha,
            f"Analysis failed: {_format_status_error(exc)}",
        )
        raise


async def handle_comment(payload: dict) -> None:
    if payload.get("action") != "created":
        return
    if "pull_request" not in payload.get("issue", {}):
        return
    await answer_comment(payload)


async def handle_workflow_run(payload: dict) -> None:
    await debug_workflow_run(payload)


async def _ensure_main_graph(
    github_client: object,
    owner: str,
    repo: str,
    base_ref: str,
    on_progress: Callable[[str, int, int, str], None] | None = None,
) -> int | None:
    if await asyncio.to_thread(code_graph_has_files, owner, repo):
        return None
    logger.info("Main graph missing for %s/%s; bootstrapping from PR base %s", owner, repo, base_ref)
    files = await asyncio.to_thread(fetch_repository_files, github_client, owner, repo, base_ref)
    return await asyncio.to_thread(build_repo_graph, owner, repo, files, on_progress)


async def _post_pr_status_comment(
    github_client: object,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    status: str,
) -> None:
    marker = _pr_status_marker(pr_number, head_sha)
    body = _pr_status_body(status)
    await asyncio.to_thread(_post_pr_status_comment_sync, github_client, owner, repo, pr_number, marker, body)


def _post_pr_status_comment_sync(
    github_client: object,
    owner: str,
    repo: str,
    pr_number: int,
    marker: str,
    body: str,
) -> None:
    post_or_update_pr_comment(github_client, owner, repo, pr_number, marker, body)


def _pr_status_marker(pr_number: int, head_sha: str) -> str:
    return f"<!-- graphora-pr-status:{pr_number}:{head_sha} -->"


def _pr_status_body(status: str) -> str:
    pr_graph = "PENDING"
    review = "PENDING"
    pr_progress = "-"
    review_progress = "-"
    if status == "Building PR graph":
        pr_graph = "RUNNING"
    elif status.startswith("PR graph progress:"):
        pr_graph = "RUNNING"
        pr_progress = _status_progress_text(status)
    elif status.startswith("PR graph ready"):
        pr_graph = "DONE"
        pr_progress = "complete"
        review = "RUNNING"
    elif status == "Review complete":
        pr_graph = "DONE"
        pr_progress = "complete"
        review = "DONE"
        review_progress = "complete"
    elif status.startswith("Analysis failed:"):
        pr_graph = "FAILED"
        review = "SKIPPED"

    return "\n".join(
        [
            "## Graphora Analysis Status",
            "",
            "| Step | Status | Progress |",
            "| --- | --- | --- |",
            f"| PR overlay graph | **{pr_graph}** | {pr_progress} |",
            f"| Review agent | **{review}** | {review_progress} |",
            "",
            f"> {status}",
        ]
    )


def _format_status_error(error: Exception) -> str:
    message = str(error).replace("\n", " ").strip()
    if len(message) > 500:
        message = f"{message[:497]}..."
    return message or error.__class__.__name__


def _status_progress_text(status: str) -> str:
    return status.split(":", 1)[1].strip() if ":" in status else "-"


class _LogProgressReporter:
    """Reports graph build progress to the server terminal logs only.

    Used for the main repository graph so its build/init progress stays in the
    server logs and is never surfaced inside PR comments.
    """

    def __init__(self, owner: str, repo: str, label: str, file_interval: int = 25) -> None:
        self.owner = owner
        self.repo = repo
        self.label = label
        self.file_interval = file_interval
        self.last_logged = 0

    def __call__(self, stage: str, current: int, total: int, path: str) -> None:
        if stage == "complete":
            logger.info("%s build complete for %s/%s (%s files)", self.label, self.owner, self.repo, total)
            return
        if current - self.last_logged < self.file_interval and current not in (1, total):
            return
        self.last_logged = current
        logger.info(
            "%s build for %s/%s: %s/%s - %s %s",
            self.label,
            self.owner,
            self.repo,
            current,
            total,
            stage,
            path,
        )


class _PrProgressReporter:
    def __init__(
        self,
        github_client: object,
        owner: str,
        repo: str,
        pr_number: int,
        head_sha: str,
        graph_name: str,
        min_interval_seconds: float = 15,
        file_interval: int = 5,
    ) -> None:
        self.github_client = github_client
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.head_sha = head_sha
        self.graph_name = graph_name
        self.min_interval_seconds = min_interval_seconds
        self.file_interval = file_interval
        self.last_update_at = 0.0
        self.last_file_count = 0

    def __call__(self, stage: str, current: int, total: int, path: str) -> None:
        if not self._should_update(current, total):
            return
        label = "Main graph" if self.graph_name == "Main graph" else "PR graph"
        progress = f"{current} / {total} files"
        if path:
            progress = f"{progress} - {stage}: `{path}`"
        else:
            progress = f"{progress} - {stage}"
        marker = _pr_status_marker(self.pr_number, self.head_sha)
        body = _pr_status_body(f"{label} progress: {progress}")
        _post_pr_status_comment_sync(self.github_client, self.owner, self.repo, self.pr_number, marker, body)
        self.last_update_at = time.monotonic()
        self.last_file_count = current

    def _should_update(self, current: int, total: int) -> bool:
        if current <= 1 or current >= total:
            return True
        if current - self.last_file_count >= self.file_interval:
            return True
        return time.monotonic() - self.last_update_at >= self.min_interval_seconds


def _changed_paths_from_commits(commits: list[dict]) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for commit in commits:
        for key in ("added", "modified"):
            for path in commit.get(key, []):
                if path not in seen:
                    seen.add(path)
                    paths.append(path)
    return paths


def _pull_request_diff(pr: object) -> str:
    parts: list[str] = []
    for file in pr.get_files():
        patch = getattr(file, "patch", None)
        if patch:
            parts.append(f"diff --git a/{file.filename} b/{file.filename}\n{patch}")
    return "\n\n".join(parts)


def _pull_request_changed_files(repo_obj: object, pr: object) -> list[dict[str, str]]:
    from github.GithubException import GithubException

    changed_files: list[dict[str, str]] = []
    head_sha = pr.head.sha
    for file in pr.get_files():
        if getattr(file, "status", "") == "removed":
            continue
        try:
            content_file = repo_obj.get_contents(file.filename, ref=head_sha)
        except GithubException as exc:
            patch_content = _content_from_patch(getattr(file, "patch", None))
            if not patch_content:
                logger.info("Skipping unavailable PR file %s at %s: %s", file.filename, head_sha, exc)
                continue
            logger.info("Using PR patch fallback for %s at %s after content fetch failed: %s", file.filename, head_sha, exc)
            changed_files.append({"path": file.filename, "content": patch_content})
        else:
            changed_files.append(
                {
                    "path": content_file.path,
                    "content": content_file.decoded_content.decode("utf-8", errors="replace"),
                }
            )
    return changed_files


def _content_from_patch(patch: str | None) -> str:
    if not patch:
        return ""
    added_lines = []
    for line in patch.splitlines():
        if line.startswith("+++") or not line.startswith("+"):
            continue
        added_lines.append(line[1:])
    return "\n".join(added_lines)


def _repository_name(payload: dict) -> str:
    repository = payload.get("repository") or {}
    full_name = repository.get("full_name")
    if full_name:
        return str(full_name)
    owner = repository.get("owner", {}).get("login")
    name = repository.get("name")
    if owner and name:
        return f"{owner}/{name}"
    return "unknown"
