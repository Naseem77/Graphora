from __future__ import annotations

from app.config import Settings, get_settings
from app.graph.diff import diff_main_vs_pr
from app.graph.store import symbol_impact
from app.parser.treesitter import extract_symbols_from_diff


def build_pr_context(owner: str, repo: str, diff: str, pr_number: int | None = None, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    symbols = extract_symbols_from_diff(diff)

    structured_diff = (
        diff_main_vs_pr(owner, repo, pr_number, settings).to_prompt_context()
        if pr_number is not None
        else ""
    )
    impacts = symbol_impact(owner, repo, symbols, settings, suffix="main")

    parts: list[str] = []
    if structured_diff:
        parts.append(structured_diff)
    parts.append(render_impact(impacts, symbols))
    return "\n\n".join(part for part in parts if part)


def render_impact(impacts: list[dict], symbols: list[str] | None = None) -> str:
    if not impacts:
        if symbols:
            return (
                "Codebase impact (structural graph): no callers, callees, or tests found "
                f"for changed symbols ({', '.join(symbols)})."
            )
        return "Codebase impact (structural graph): no changed symbols detected."

    lines = ["Codebase impact (structural graph):"]
    for impact in impacts:
        lines.append("")
        lines.append(f"### `{impact['name']}`")
        for definition in impact["definitions"]:
            location = _location(definition.get("path"), definition.get("line"))
            kind = definition.get("kind") or "symbol"
            signature = (definition.get("signature") or "").strip()
            suffix = f" — `{signature}`" if signature else ""
            lines.append(f"- defined as {kind} at {location}{suffix}")
        lines.append(f"- callers ({len(impact['callers'])}): {_refs(impact['callers'])}")
        lines.append(f"- calls ({len(impact['callees'])}): {_refs(impact['callees'])}")
        lines.append(f"- tests ({len(impact['tests'])}): {_refs(impact['tests'])}")
    return "\n".join(lines)


def _refs(refs: list[dict]) -> str:
    if not refs:
        return "none"
    return ", ".join(f"`{ref['name']}` ({ref['path']})" for ref in refs)


def _location(path: str | None, line: object) -> str:
    if not path:
        return "unknown"
    if line in (None, ""):
        return f"`{path}`"
    return f"`{path}:{line}`"
