from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Awaitable, Callable

from app.config import get_settings
from app.github.app import get_github_client
from app.graph.builder import build_repo_graph, fetch_repository_files
from app.graph.pr_graph import build_pr_graph, delete_pr_graph
from app.graph.updater import update_graph_for_push
from app.review.chat import answer_comment
from app.review.reviewer import review_and_post_pr


logger = logging.getLogger(__name__)

WebhookHandler = Callable[[dict], Awaitable[None]]


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    secret = get_settings().github_webhook_secret
    if not secret:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is required to verify webhooks")
    if not signature_header:
        return False

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


async def dispatch_webhook(event: str, payload: dict) -> None:
    handlers: dict[str, WebhookHandler] = {
        "installation": handle_install,
        "push": handle_push,
        "pull_request": handle_pr,
        "issue_comment": handle_comment,
    }
    handler = handlers.get(event)
    if handler is None:
        logger.info("Ignoring unsupported GitHub event: %s", event)
        return
    await handler(payload)


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
        files = fetch_repository_files(github_client, owner, repo)
        build_repo_graph(owner, repo, files)


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

    update_graph_for_push(owner, repo, changed_files)


async def handle_pr(payload: dict) -> None:
    action = payload.get("action")
    if action == "closed":
        repository = payload["repository"]
        owner = repository["owner"]["login"]
        repo = repository["name"]
        pr_number = payload["pull_request"]["number"]
        delete_pr_graph(owner, repo, pr_number)
        return

    if action not in {"opened", "synchronize", "reopened", "ready_for_review"}:
        return

    installation_id = payload["installation"]["id"]
    repository = payload["repository"]
    owner = repository["owner"]["login"]
    repo = repository["name"]
    pr_number = payload["pull_request"]["number"]

    github_client = get_github_client(installation_id)
    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    pr = repo_obj.get_pull(pr_number)
    changed_files = _pull_request_changed_files(repo_obj, pr)
    build_pr_graph(owner, repo, pr_number, changed_files)
    diff = _pull_request_diff(pr)
    await review_and_post_pr(github_client, owner, repo, pr_number, diff)


async def handle_comment(payload: dict) -> None:
    if payload.get("action") != "created":
        return
    if "pull_request" not in payload.get("issue", {}):
        return
    await answer_comment(payload)


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
