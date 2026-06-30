from __future__ import annotations

from dataclasses import dataclass

COMMANDS = {"help", "review", "pause", "resume", "summary"}


@dataclass(frozen=True)
class ParsedCommand:
    name: str | None
    argument: str


def parse_command(text: str) -> ParsedCommand:
    """Parse the text following a bot mention into a command.

    Returns a command name when the first word is a known command, otherwise
    ``name is None`` and the original text is treated as a free-form question.
    """
    stripped = text.strip()
    if not stripped:
        return ParsedCommand(None, "")
    first, _, rest = stripped.partition(" ")
    token = first.lower().lstrip("/")
    if token in COMMANDS:
        return ParsedCommand(token, rest.strip())
    return ParsedCommand(None, stripped)


def help_text(bot_login: str) -> str:
    return "\n".join(
        [
            f"### @{bot_login} commands",
            "",
            f"- `@{bot_login} review` — run a fresh graph-powered review of this PR.",
            f"- `@{bot_login} summary` — post a summary/walkthrough of this PR.",
            f"- `@{bot_login} pause` — stop automatic reviews on new commits.",
            f"- `@{bot_login} resume` — re-enable automatic reviews.",
            f"- `@{bot_login} help` — show this message.",
            f"- `@{bot_login} <question>` — ask anything about the code using the knowledge graph.",
        ]
    )
