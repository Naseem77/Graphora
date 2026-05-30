from __future__ import annotations

from app.graph.sdk import get_kg
from app.parser.treesitter import extract_symbols_from_diff


def build_pr_context(owner: str, repo: str, diff: str) -> str:
    symbols = extract_symbols_from_diff(diff)
    if symbols:
        target = ", ".join(symbols)
        question = (
            "What functions, classes, files, and tests depend on or call any of these symbols: "
            f"{target}? Focus on risks relevant to a pull request review."
        )
    else:
        question = (
            "Analyze this pull request diff and identify related files, callers, dependencies, "
            "and tests that are relevant to review:\n\n"
            f"{diff[:8000]}"
        )

    return str(get_kg(owner, repo).chat_session().ask(question))
