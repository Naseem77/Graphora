from __future__ import annotations

import re

import litellm

from app.config import Settings, get_settings
from app.graph.pr_context import render_impact
from app.graph.store import code_graph_stats, symbol_impact


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "what", "which", "where", "does", "this", "that",
    "from", "into", "have", "how", "why", "are", "was", "will", "can", "should",
    "graph", "code", "file", "files", "function", "functions", "class", "classes",
    "pull", "request", "review", "changes", "change", "about", "tell", "show",
}


def answer_repo_question(owner: str, repo: str, question: str, settings: Settings | None = None) -> str:
    """Answer a free-form question grounded in the structural code graph.

    Candidate identifiers in the question are looked up in the graph for impact
    (definitions/callers/callees/tests); that structural context plus a repo
    overview is passed to the review LLM. No GraphRAG retrieval is used.
    """
    settings = settings or get_settings()
    context = _graph_context(owner, repo, question, settings)
    return _ask_llm(question, context, settings)


def _graph_context(owner: str, repo: str, question: str, settings: Settings) -> str:
    candidates = [
        token
        for token in dict.fromkeys(_IDENTIFIER.findall(question))
        if token.lower() not in _STOPWORDS
    ]
    parts: list[str] = []
    try:
        stats = code_graph_stats(owner, repo, settings)
        counts = ", ".join(f"{label}: {count}" for label, count in sorted(stats["node_counts"].items()))
        parts.append(
            f"Repository graph overview: {stats['node_total']} nodes ({counts}), "
            f"{stats['relationship_count']} relationships, {len(stats['files'])} files."
        )
    except Exception:  # noqa: BLE001 - context is best-effort
        pass

    if candidates:
        try:
            impacts = symbol_impact(owner, repo, candidates, settings, suffix="main")
            parts.append(render_impact(impacts, candidates))
        except Exception:  # noqa: BLE001
            pass

    return "\n\n".join(parts) if parts else "No structural graph context available."


def _ask_llm(question: str, context: str, settings: Settings) -> str:
    if not (settings.azure_api_key and settings.azure_api_base and settings.review_model):
        return (
            "I can't answer right now because the review model is not configured. "
            "Here is the structural graph context I found:\n\n" + context
        )

    prompt = (
        "You are a code assistant answering a question about a repository using ONLY the "
        "structural code-graph context provided. Be concise and specific. If the context does "
        "not contain the answer, say so.\n\n"
        f"## Question\n{question}\n\n## Structural graph context\n{context}\n"
    )
    response = litellm.completion(
        model=settings.review_model,
        api_key=settings.azure_api_key,
        api_base=settings.azure_api_base,
        api_version=settings.azure_api_version,
        max_tokens=1024,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return str(response.choices[0].message.content or "").strip()
