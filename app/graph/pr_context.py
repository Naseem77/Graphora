from __future__ import annotations

from app.graph.diff import diff_main_vs_pr
from app.graph.sdk import get_kg
from app.graph.pr_graph import pr_suffix
from app.parser.treesitter import extract_symbols_from_diff


def build_pr_context(owner: str, repo: str, diff: str, pr_number: int | None = None) -> str:
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

    structured_diff = diff_main_vs_pr(owner, repo, pr_number).to_prompt_context() if pr_number is not None else ""
    main_context = str(get_kg(owner, repo).chat_session().ask(question))
    if pr_number is None:
        return f"Main graph context:\n{main_context}"

    pr_context = str(get_kg(owner, repo, suffix=pr_suffix(pr_number)).chat_session().ask(question))
    return f"{structured_diff}\n\nMain graph context:\n{main_context}\n\nPR overlay graph context:\n{pr_context}"
