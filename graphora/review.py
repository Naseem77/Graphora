"""Grounded diff review.

Works in two modes:
- facts mode (default, zero LLM): prints the deterministic blast radius,
  graph diff facts, and risk memory for a change.
- LLM mode (optional): sends diff + compact graph context to a model via
  litellm and returns a structured review. The graph context is the only
  repo knowledge the model gets, which is what keeps token cost tiny.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from graphora.blast import BlastRadius, blast_radius_for_diff
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

    def render(self) -> str:
        lines = ["# Graphora Review", "", self.summary.strip()]
        if self.findings:
            lines += ["", "## Findings"]
            for finding in self.findings:
                location = f"`{finding.path}`" + (f":{finding.line}" if finding.line else "")
                lines.append(f"- **{finding.severity}** {location}: {finding.body}")
        lines += ["", "## Grounded facts (from the graph)", "", "```", self.grounded_facts, "```"]
        lines.append(f"\n_Context sent to model: ~{self.context_tokens} tokens (graph blast radius, not the repo)._")
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
    radius = blast_radius_for_diff(store, diff)
    facts = radius.to_context()
    risky = _risk_warnings(radius)
    context_tokens = estimate_tokens(diff) + estimate_tokens(facts)

    model = model or os.environ.get("REVIEW_MODEL", "")
    if not use_llm or not model:
        summary = _facts_summary(radius)
        return Review(summary=summary, findings=risky, context_tokens=context_tokens, grounded_facts=facts)

    raw = _ask_llm(diff, facts, model)
    summary, findings = _parse_llm_review(raw)
    return Review(
        summary=summary,
        findings=risky + findings,
        context_tokens=context_tokens,
        grounded_facts=facts,
    )


def _facts_summary(radius: BlastRadius) -> str:
    parts = []
    for impact in radius.symbols:
        caller_count = len(impact.callers)
        test_count = len(impact.tests)
        note = f"`{impact.name}` has {caller_count} caller(s) and {test_count} covering test(s)."
        if test_count == 0 and caller_count > 0:
            note += " No test coverage on a symbol with callers."
        parts.append(note)
    if not parts:
        return "No known symbols changed (graph found nothing to ground)."
    return " ".join(parts)


def _risk_warnings(radius: BlastRadius) -> list[ReviewFinding]:
    findings = []
    for impact in radius.symbols:
        if impact.fix_count > 0:
            findings.append(
                ReviewFinding(
                    path=impact.path,
                    line=impact.line,
                    severity="risk-memory",
                    body=(
                        f"`{impact.name}` was involved in {impact.fix_count} past fix/revert commits "
                        f"(last: {impact.last_broke_at[:10]}), risk score {impact.risk_score:.2f}, "
                        f"{len(impact.callers)} caller(s). Review extra carefully."
                    ),
                )
            )
        if not impact.tests and impact.callers:
            findings.append(
                ReviewFinding(
                    path=impact.path,
                    line=impact.line,
                    severity="warning",
                    body=f"`{impact.name}` has {len(impact.callers)} caller(s) but zero covering tests in the graph.",
                )
            )
    return findings


def _ask_llm(diff: str, facts: str, model: str) -> str:
    import litellm

    prompt = f"""You are a senior code reviewer. You get a diff plus deterministic facts
from a code knowledge graph (callers, callees, tests, risk history). Ground every claim
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
