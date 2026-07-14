"""Skill installer: teach any AI coding agent to use Graphora.

`graphora install-skill all` writes a small rule/skill file into each
agent's project-level config location. The rule tells the agent to check
the blast radius before editing a symbol and to run a grounded review
before committing. One command, 20+ agents.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MARK_START = "<!-- graphora:start -->"
MARK_END = "<!-- graphora:end -->"

SKILL_BODY = """\
# Graphora: code graph, blast radius, and risk memory

This repository is indexed by Graphora, a deterministic code knowledge graph
(tree-sitter + graph storage, zero LLM to build) with a risk memory mined from
git history. Use it before you edit, and before you commit.

## Workflow

1. **Before editing a function or class**, check who depends on it:
   `graphora blast <symbol>` shows callers, callees, covering tests, and
   importers, each tagged EXTRACTED, INFERRED, or AMBIGUOUS.
2. **Before committing**, run a grounded review of your diff:
   `git diff | graphora review` flags changed symbols that have callers but
   no covering tests, and warns when a symbol broke before (risk memory).
3. **When choosing what to test or refactor first**:
   `graphora risk top` ranks symbols by historical fix/revert frequency.
4. **Keep the graph fresh** after adding or deleting files:
   `graphora index .`

## Notes

- Works with zero setup: without a FalkorDB server, Graphora automatically
  uses its embedded backend (`--backend embedded` to force it).
- Prefer facts from the graph over guessing: a blast radius answer is read
  from real structure, not inferred from text.
- MCP alternative: `graphora serve-mcp` exposes blast_radius, review_diff,
  risk_top, find_symbol, and graph_stats as MCP tools.
"""


@dataclass(frozen=True)
class SkillTarget:
    agent: str
    path: str            # relative to the repo root
    style: str           # "file" (own file) or "block" (marked block in a shared file)
    frontmatter: str = ""


SKILL_TARGETS: list[SkillTarget] = [
    SkillTarget(
        "claude-code",
        ".claude/skills/graphora/SKILL.md",
        "file",
        frontmatter=(
            "---\n"
            "name: graphora\n"
            "description: Check the Graphora code graph (blast radius, risk memory, grounded review) before editing or committing code in this repository.\n"
            "---\n\n"
        ),
    ),
    SkillTarget(
        "cursor",
        ".cursor/rules/graphora.mdc",
        "file",
        frontmatter=(
            "---\n"
            "description: Graphora code graph workflow (blast radius, risk memory, grounded review)\n"
            "alwaysApply: true\n"
            "---\n\n"
        ),
    ),
    SkillTarget(
        "copilot",
        ".github/instructions/graphora.instructions.md",
        "file",
        frontmatter="---\napplyTo: \"**\"\n---\n\n",
    ),
    SkillTarget("windsurf", ".windsurf/rules/graphora.md", "file"),
    SkillTarget("cline", ".clinerules/graphora.md", "file"),
    SkillTarget("roo-code", ".roo/rules/graphora.md", "file"),
    SkillTarget("continue", ".continue/rules/graphora.md", "file"),
    SkillTarget("amazonq", ".amazonq/rules/graphora.md", "file"),
    SkillTarget("trae", ".trae/rules/graphora.md", "file"),
    SkillTarget("kilocode", ".kilocode/rules/graphora.md", "file"),
    SkillTarget("augment", ".augment/rules/graphora.md", "file"),
    SkillTarget("kiro", ".kiro/steering/graphora.md", "file"),
    SkillTarget("junie", ".junie/guidelines.md", "block"),
    SkillTarget("codex", "AGENTS.md", "block"),
    SkillTarget("opencode", "AGENTS.md", "block"),
    SkillTarget("copilot-cli", "AGENTS.md", "block"),
    SkillTarget("gemini-cli", "GEMINI.md", "block"),
    SkillTarget("claude-md", "CLAUDE.md", "block"),
    SkillTarget("zed", ".rules", "block"),
    SkillTarget("goose", ".goosehints", "block"),
    SkillTarget("aider", "CONVENTIONS.md", "block"),
    SkillTarget("void", ".voidrules", "block"),
]


def list_agents() -> list[str]:
    return [t.agent for t in SKILL_TARGETS]


def install_skill(repo_root: str | Path, agents: list[str] | None = None) -> list[str]:
    """Write the Graphora skill for the given agents (default: all).

    Returns the list of files written, relative to the repo root.
    Idempotent: own files are overwritten, marked blocks are replaced in place.
    """
    root = Path(repo_root).resolve()
    wanted = set(agents) if agents else set(list_agents())
    unknown = wanted - set(list_agents())
    if unknown:
        raise ValueError(f"Unknown agents: {sorted(unknown)}. Known: {list_agents()}")

    written: list[str] = []
    for target in SKILL_TARGETS:
        if target.agent not in wanted:
            continue
        path = root / target.path
        if target.style == "file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(target.frontmatter + SKILL_BODY, encoding="utf-8")
        else:
            _write_block(path)
        rel = target.path
        if rel not in written:
            written.append(rel)
    return written


def _write_block(path: Path) -> None:
    block = f"{MARK_START}\n{SKILL_BODY}{MARK_END}\n"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if MARK_START in text and MARK_END in text:
            head, rest = text.split(MARK_START, 1)
            _, tail = rest.split(MARK_END, 1)
            path.write_text(head + block.rstrip("\n") + tail, encoding="utf-8")
            return
        separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        path.write_text(text + separator + block, encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block, encoding="utf-8")
