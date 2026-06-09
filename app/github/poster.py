from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github import Github


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
