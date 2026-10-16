"""Grounded diff review.

Works in two modes:
- facts mode (default, zero LLM): what the change could break, split into
  certain and uncertain callers (with the alternatives of each uncertain
  call), plus warnings for changed code that no test reaches.
- LLM mode (optional): sends diff + compact graph context to a model via
  litellm and returns a structured review. The graph context is the only
  repo knowledge the model gets, which is what keeps token cost tiny.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from graphora.impact import WhatCouldBreak, what_could_break_for_diff
from graphora.store import GraphStore


@dataclass(frozen=True)
class ReviewFinding:
    path: str
    line: int | None
    severity: str
    body: str


@dataclass(frozen=True)
class Review:
    summary: str
    findings: list[ReviewFinding] = field(default_factory=list)
    context_tokens: int = 0
    grounded_facts: str = ""
    model: str = ""  # empty: facts mode, nothing was sent anywhere

    def render(self) -> str:
        lines = ["# Graphora Review", "", self.summary.strip()]
        if self.findings:
            lines += ["", "## Findings"]
            for finding in self.findings:
                location = f"`{finding.path}`" + (f":{finding.line}" if finding.line else "")
                lines.append(f"- **{finding.severity}** {location}: {finding.body}")
        lines += ["", "## Grounded facts (from the graph)", "", "```", self.grounded_facts, "```"]
        if self.model:
            lines.append(f"\n_Context sent to {self.model}: ~{self.context_tokens} tokens (graph facts, not the repo)._")
        else:
            lines.append("\n_Facts mode: no model was called._")
        return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (~4 chars/token heuristic)."""
    return max(1, len(text) // 4)


def review_diff(
    store: GraphStore,
    diff: str,
    model: str | None = None,
    use_llm: bool = True,
) -> Review:
    impact = what_could_break_for_diff(store, diff)
    facts = impact.to_text()
    warnings = _missing_test_warnings(impact)
    context_tokens = estimate_tokens(diff) + estimate_tokens(facts)

    model = model or os.environ.get("REVIEW_MODEL", "")
    if not use_llm or not model:
        return Review(summary=_facts_summary(impact), findings=warnings, context_tokens=context_tokens,
                      grounded_facts=facts)

    raw = _ask_llm(diff, facts, model)
    summary, findings = _parse_llm_review(raw)
    return Review(
        summary=summary,
        findings=warnings + findings,
        context_tokens=context_tokens,
        grounded_facts=facts,
        model=model,
    )


def _facts_summary(impact: WhatCouldBreak) -> str:
    if not impact.targets:
        return "No known symbols changed (graph found nothing to ground)."
    changed = ", ".join(f"`{t['qualname']}`" for t in impact.targets[:8])
    if len(impact.targets) > 8:
        changed += f" and {len(impact.targets) - 8} more"
    return (
        f"Changed: {changed}. Will break: {len(impact.will_break)} caller(s); "
        f"might break: {len(impact.might_break)} (ambiguous calls); "
        f"tests that reach the change: {len(impact.tests)}."
    )


def _missing_test_warnings(impact: WhatCouldBreak) -> list[ReviewFinding]:
    tested = {b.calls for b in impact.tests if b.depth == 1}
    affected = {b.calls for b in impact.will_break if b.depth == 1}
    findings = []
    for target in impact.targets:
        name = target["qualname"]
        if name in affected and name not in tested:
            callers = sum(1 for b in impact.will_break if b.depth == 1 and b.calls == name)
            findings.append(
                ReviewFinding(
                    path=target["path"],
                    line=target["line"],
                    severity="warning",
                    body=f"`{name}` has {callers} caller(s) but no test calls it directly in the graph.",
                )
            )
    return findings


def _ask_llm(diff: str, facts: str, model: str) -> str:
    import litellm

    prompt = f"""You are a senior code reviewer. You get a diff plus deterministic facts
from a code knowledge graph (what the change could break, and tests). Ground every claim
in those facts. Respond with a SINGLE JSON object:
{{"summary": "...", "findings": [{{"path": "...", "line": 1, "severity": "critical|warning|suggestion", "body": "..."}}]}}

## Diff
{diff}

## Graph facts
{facts}
"""
    response = litellm.completion(
        model=model,
        max_tokens=2048,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return str(response.choices[0].message.content or "")


def _parse_llm_review(raw: str) -> tuple[str, list[ReviewFinding]]:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return text, []
    try:
        payload = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return text, []
    findings = [
        ReviewFinding(
            path=str(item.get("path", "")),
            line=int(item["line"]) if str(item.get("line", "")).isdigit() else None,
            severity=str(item.get("severity", "suggestion")),
            body=str(item.get("body", "")),
        )
        for item in payload.get("findings", [])
        if isinstance(item, dict) and item.get("path")
    ]
    return str(payload.get("summary", "")), findings
