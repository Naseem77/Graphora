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
