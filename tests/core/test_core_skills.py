"""Skill installer tests: 20+ agents, idempotent, correct formats."""

import json
from pathlib import Path

import pytest

from graphora.skills import (
    MARK_END,
    MARK_START,
    SKILL_TARGETS,
    install_skill,
    list_agents,
)


def test_supports_at_least_20_agents():
    agents = list_agents()
    assert len(agents) >= 20
    assert len(set(agents)) == len(agents)  # no duplicates


def test_install_all_writes_every_target(tmp_path: Path):
    written = install_skill(tmp_path)
    unique_paths = {t.path for t in SKILL_TARGETS}
    assert set(written) == unique_paths
    for rel in written:
        text = (tmp_path / rel).read_text()
        assert "graphora blast" in text
        assert "graphora review" in text


def test_claude_skill_has_frontmatter(tmp_path: Path):
    install_skill(tmp_path, ["claude-code"])
    text = (tmp_path / ".claude/skills/graphora/SKILL.md").read_text()
    assert text.startswith("---\nname: graphora\n")


def test_cursor_rule_has_frontmatter(tmp_path: Path):
    install_skill(tmp_path, ["cursor"])
    text = (tmp_path / ".cursor/rules/graphora.mdc").read_text()
    assert "alwaysApply: true" in text.split("---")[1]


def test_block_appends_and_preserves_existing_content(tmp_path: Path):
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# My project rules\n\nAlways use type hints.\n")
    install_skill(tmp_path, ["codex"])
    text = agents_md.read_text()
    assert text.startswith("# My project rules")
    assert "Always use type hints." in text
    assert MARK_START in text and MARK_END in text


def test_block_install_is_idempotent(tmp_path: Path):
    install_skill(tmp_path, ["codex"])
    first = (tmp_path / "AGENTS.md").read_text()
    install_skill(tmp_path, ["codex"])
    second = (tmp_path / "AGENTS.md").read_text()
    assert first == second
    assert second.count(MARK_START) == 1


def test_shared_file_written_once_for_multiple_agents(tmp_path: Path):
    written = install_skill(tmp_path, ["codex", "opencode", "copilot-cli"])
    assert written == ["AGENTS.md"]
    assert (tmp_path / "AGENTS.md").read_text().count(MARK_START) == 1


def test_unknown_agent_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="Unknown agents"):
        install_skill(tmp_path, ["clippy"])


def test_cli_install_skill_end_to_end(tmp_path: Path, capsys):
    from graphora.cli import main

    assert main(["install-skill", "claude-code", "cursor", "--repo", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(out["written"]) == [".claude/skills/graphora/SKILL.md", ".cursor/rules/graphora.mdc"]
    assert (tmp_path / ".claude/skills/graphora/SKILL.md").exists()


def test_cli_install_skill_list(capsys):
    from graphora.cli import main

    assert main(["install-skill", "--list"]) == 0
    listed = capsys.readouterr().out.strip().splitlines()
    assert "claude-code" in listed and len(listed) >= 20


def test_cli_install_skill_unknown_agent_fails(tmp_path: Path, capsys):
    from graphora.cli import main

    assert main(["install-skill", "clippy", "--repo", str(tmp_path)]) == 1


def test_install_sessions_skill_writes_own_files(tmp_path: Path):
    written = install_skill(tmp_path, ["claude-code", "cursor"], skill="sessions")
    assert ".claude/skills/graphora-sessions/SKILL.md" in written
    assert ".cursor/rules/graphora-sessions.mdc" in written
    text = (tmp_path / ".claude/skills/graphora-sessions/SKILL.md").read_text()
    assert "name: graphora-sessions" in text
    assert "sessions ingest" in text
    assert "cross-agent" in text.lower()


def test_sessions_and_code_skills_coexist_in_shared_file(tmp_path: Path):
    install_skill(tmp_path, ["codex"], skill="code")
    install_skill(tmp_path, ["codex"], skill="sessions")
    text = (tmp_path / "AGENTS.md").read_text()
    assert "<!-- graphora:start -->" in text
    assert "<!-- graphora-sessions:start -->" in text
    # re-install must not duplicate
    install_skill(tmp_path, ["codex"], skill="sessions")
    assert text.count("graphora-sessions:start") == (tmp_path / "AGENTS.md").read_text().count("graphora-sessions:start")


def test_unknown_skill_kind_raises(tmp_path: Path):
    import pytest as _pytest

    with _pytest.raises(ValueError):
        install_skill(tmp_path, ["codex"], skill="nope")


def test_cli_install_both_skills(tmp_path: Path, capsys):
    from graphora.cli import main

    assert main(["install-skill", "copilot-cli", "--repo", str(tmp_path), "--skill", "all"]) == 0
    text = (tmp_path / "AGENTS.md").read_text()
    assert "<!-- graphora:start -->" in text
    assert "<!-- graphora-sessions:start -->" in text
