from __future__ import annotations

import re

from app.config import Settings, get_settings
from app.github.app import get_github_client
from app.github.poster import post_issue_comment
from app.graph.sdk import get_kg


async def answer_comment(payload: dict, settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    body = payload.get("comment", {}).get("body", "")
    mention = f"@{settings.bot_login}"
    if mention.lower() not in body.lower():
        return None

    question = re.sub(re.escape(mention), "", body, count=1, flags=re.IGNORECASE).strip()
    if not question:
        question = "Summarize the graph context for this pull request."

    repository = payload["repository"]
    owner = repository["owner"]["login"]
    repo = repository["name"]
    issue_number = payload["issue"]["number"]
    installation_id = payload["installation"]["id"]

    answer = str(get_kg(owner, repo).chat_session().ask(question))
    github_client = get_github_client(installation_id, settings)
    post_issue_comment(github_client, owner, repo, issue_number, f"**@{settings.bot_login}** {answer}")
    return answer
