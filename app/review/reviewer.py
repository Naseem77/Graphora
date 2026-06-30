from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import litellm

from app.config import Settings, get_settings
from app.graph.pr_context import build_pr_context
from app.github.poster import post_inline_review_comments, post_or_update_pr_comment
from app.parser.treesitter import extract_symbols_from_diff


REVIEW_HEADER = "## GraphReview Analysis"
REVIEW_MARKER = "<!-- graphora-review -->"

_SEVERITY_ICONS = {
    "critical": "🛑",
    "warning": "⚠️",
    "suggestion": "💡",
    "nitpick": "🔧",
}


@dataclass(frozen=True)
class FileNote:
    path: str
    summary: str


@dataclass(frozen=True)
class InlineComment:
    path: str
    line: int | None
    severity: str
    body: str


@dataclass(frozen=True)
class StructuredReview:
    summary: str
    walkthrough: list[FileNote] = field(default_factory=list)
    comments: list[InlineComment] = field(default_factory=list)


async def review_and_post_pr(
    github_client: object,
    owner: str,
    repo: str,
    pr_number: int,
    diff: str,
    settings: Settings | None = None,
) -> StructuredReview:
    review = await review_pr(owner, repo, pr_number, diff, settings)
    body = render_review_comment(review)
    await asyncio.to_thread(
        post_or_update_pr_comment, github_client, owner, repo, pr_number, REVIEW_MARKER, body
    )
    inline = [_inline_payload(comment) for comment in review.comments if comment.line]
    if inline:
        await asyncio.to_thread(
            post_inline_review_comments, github_client, owner, repo, pr_number, inline
        )
    return review


async def review_pr(owner: str, repo: str, pr_number: int, diff: str, settings: Settings | None = None) -> StructuredReview:
    settings = settings or get_settings()
    context = await asyncio.to_thread(build_pr_context, owner, repo, diff, pr_number, settings)
    raw = await asyncio.to_thread(_ask_llm_for_review, diff, context, settings)
    return _parse_review(raw)


def render_review_comment(review: StructuredReview) -> str:
    lines = [REVIEW_HEADER, "", review.summary.strip() or "No summary produced."]

    if review.walkthrough:
        lines += ["", "### Walkthrough", "", "| File | Summary |", "| --- | --- |"]
        for note in review.walkthrough:
            lines.append(f"| `{note.path}` | {_one_line(note.summary)} |")

    if review.comments:
        lines += ["", "### Findings", ""]
        for comment in review.comments:
            icon = _SEVERITY_ICONS.get(comment.severity.lower(), "•")
            location = f"`{comment.path}`"
            if comment.line:
                location += f":{comment.line}"
            lines.append(f"- {icon} **{comment.severity.title()}** {location} — {_one_line(comment.body)}")
    else:
        lines += ["", "_No blocking issues found._"]

    return "\n".join(lines)


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
    prompt = f"""You are a senior code reviewer, like CodeRabbit, reviewing a pull request using a
code knowledge graph for context. Respond with a SINGLE JSON object and nothing else, matching:

{{
  "summary": "A concise markdown walkthrough of what this PR does and its overall risk.",
  "walkthrough": [{{"path": "file/path", "summary": "one-line description of the change"}}],
  "comments": [
    {{"path": "file/path", "line": 12, "severity": "critical|warning|suggestion|nitpick",
      "comment": "actionable, specific feedback with a suggested fix"}}
  ]
}}

Rules:
- Only include genuinely meaningful comments (bugs, logic errors, breaking changes for
  callers/dependencies, missing tests, security, performance). No style nits unless severity "nitpick".
- "line" must be a line number that appears as an added (+) line in the diff, or null if not line-specific.
- If there are no issues, return an empty "comments" array and explain in "summary".

Changed symbols: {symbol_text}

## PR Diff
{diff}

## Codebase Context (knowledge graph)
{context}
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


def _parse_review(raw: str) -> StructuredReview:
    payload = _extract_json(raw)
    if payload is None:
        return StructuredReview(summary=raw.strip(), walkthrough=[], comments=[])

    walkthrough = [
        FileNote(path=str(item.get("path", "")), summary=str(item.get("summary", "")))
        for item in payload.get("walkthrough", [])
        if isinstance(item, dict) and item.get("path")
    ]
    comments = [
        InlineComment(
            path=str(item.get("path", "")),
            line=_coerce_line(item.get("line")),
            severity=str(item.get("severity", "suggestion")),
            body=str(item.get("comment", "")).strip(),
        )
        for item in payload.get("comments", [])
        if isinstance(item, dict) and item.get("path") and item.get("comment")
    ]
    return StructuredReview(summary=str(payload.get("summary", "")).strip(), walkthrough=walkthrough, comments=comments)


def _extract_json(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_line(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _inline_payload(comment: InlineComment) -> dict:
    icon = _SEVERITY_ICONS.get(comment.severity.lower(), "•")
    return {
        "path": comment.path,
        "line": comment.line,
        "body": f"{icon} **{comment.severity.title()}**: {comment.body}",
    }


def _one_line(text: str) -> str:
    return " ".join(text.split())
