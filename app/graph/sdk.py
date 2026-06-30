from __future__ import annotations


def graph_name(owner: str, repo: str, suffix: str = "main") -> str:
    return f"graph:{owner}:{repo}:{suffix}"
