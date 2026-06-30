from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github import Github


logger = logging.getLogger(__name__)


def post_issue_comment(
    github_client: "Github",
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
) -> None:
    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    repo_obj.get_issue(issue_number).create_comment(body)


def post_pr_review_comment(
    github_client: "Github",
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
) -> None:
    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    repo_obj.get_pull(pr_number).create_issue_comment(body)


def post_or_update_pr_comment(
    github_client: "Github",
    owner: str,
    repo: str,
    pr_number: int,
    marker: str,
    body: str,
) -> None:
    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    issue = repo_obj.get_issue(pr_number)
    marked_body = f"{marker}\n{body}"
    for comment in issue.get_comments():
        if marker in getattr(comment, "body", ""):
            comment.edit(marked_body)
            return
    issue.create_comment(marked_body)


def get_pr_diff(
    github_client: "Github",
    owner: str,
    repo: str,
    pr_number: int,
) -> str:
    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    pull = repo_obj.get_pull(pr_number)
    parts: list[str] = []
    for file in pull.get_files():
        patch = getattr(file, "patch", None)
        if patch:
            parts.append(f"diff --git a/{file.filename} b/{file.filename}\n{patch}")
    return "\n\n".join(parts)


def post_inline_review_comments(
    github_client: "Github",
    owner: str,
    repo: str,
    pr_number: int,
    comments: list[dict],
) -> int:
    """Post line-level review comments on a PR, best-effort.

    ``comments`` is a list of ``{"path", "line", "body"}``. Lines that GitHub
    rejects (e.g. not part of the diff) are skipped so one bad line never blocks
    the rest of the review. Returns the number of comments successfully posted.
    """
    repo_obj = github_client.get_repo(f"{owner}/{repo}")
    pull = repo_obj.get_pull(pr_number)
    commit = pull.get_commits().reversed[0]

    posted = 0
    for comment in comments:
        line = comment.get("line")
        path = comment.get("path")
        body = comment.get("body", "")
        if not path or not line:
            continue
        try:
            pull.create_review_comment(body=body, commit=commit, path=path, line=int(line), side="RIGHT")
            posted += 1
        except Exception as exc:  # noqa: BLE001 - one bad line shouldn't drop the review
            logger.info("Skipping inline comment on %s:%s: %s", path, line, exc)
    return posted
