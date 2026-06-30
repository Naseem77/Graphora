from __future__ import annotations

import asyncio
import re

from app.config import Settings, get_settings
from app.github.app import get_github_client
from app.github.poster import get_pr_diff, post_issue_comment
from app.provision.factory import settings_for_installation
from app.review.commands import help_text, parse_command
from app.review.qa import answer_repo_question
from app.review.reviewer import review_and_post_pr
from app.state import set_review_paused


async def answer_comment(payload: dict, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    comment = payload.get("comment", {})
    if comment.get("user", {}).get("type") == "Bot":
        return None

    body = comment.get("body", "")
    mention = f"@{settings.bot_login}"
    if mention.lower() not in body.lower():
        return None

    text = re.sub(re.escape(mention), "", body, count=1, flags=re.IGNORECASE).strip()

    repository = payload["repository"]
    owner = repository["owner"]["login"]
    repo = repository["name"]
    issue_number = payload["issue"]["number"]
    installation_id = payload["installation"]["id"]
    github_client = await asyncio.to_thread(get_github_client, installation_id, settings)
    graph_settings = await asyncio.to_thread(settings_for_installation, installation_id)

    command = parse_command(text)
    if command.name == "help":
        return await _reply(github_client, owner, repo, issue_number, help_text(settings.bot_login))
    if command.name == "pause":
        await asyncio.to_thread(set_review_paused, owner, repo, True)
        return await _reply(
            github_client, owner, repo, issue_number, "Automatic reviews are now **paused** for this repository."
        )
    if command.name == "resume":
        await asyncio.to_thread(set_review_paused, owner, repo, False)
        return await _reply(
            github_client, owner, repo, issue_number, "Automatic reviews are now **resumed** for this repository."
        )
    if command.name in {"review", "summary"}:
        diff = await asyncio.to_thread(get_pr_diff, github_client, owner, repo, issue_number)
        await review_and_post_pr(github_client, owner, repo, issue_number, diff, graph_settings)
        return "review"

    question = text or "Summarize the repository structure from the code graph."
    answer = await asyncio.to_thread(answer_repo_question, owner, repo, question, graph_settings)
    await asyncio.to_thread(
        post_issue_comment,
        github_client,
        owner,
        repo,
        issue_number,
        f"**@{settings.bot_login}** {answer}",
    )
    return answer


async def _reply(github_client: object, owner: str, repo: str, issue_number: int, body: str) -> str:
    await asyncio.to_thread(post_issue_comment, github_client, owner, repo, issue_number, body)
    return body
