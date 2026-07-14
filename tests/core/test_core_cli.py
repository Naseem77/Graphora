"""Phase 4 tests: CLI + facts-mode review (integration, needs FalkorDB + git)."""

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("falkordb")
from falkordb import FalkorDB  # noqa: E402

from graphora.cli import main  # noqa: E402
from graphora.store import GraphStore  # noqa: E402


def _falkordb_available() -> bool:
    try:
        FalkorDB(host="localhost", port=6379).select_graph("graphora:ping").query("RETURN 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _falkordb_available(), reason="FalkorDB not running on localhost:6379")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.dev")
    _git(tmp_path, "config", "user.name", "tester")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def compute(x):\n    return x + 1\n\ndef pipeline(x):\n    return compute(x)\n"
    )
    (tmp_path / "tests" / "test_core.py").write_text(
        "from pkg.core import compute\n\ndef test_compute():\n    assert compute(1) == 2\n"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    (tmp_path / "pkg" / "core.py").write_text(
        "def compute(x):\n    return x + 2\n\ndef pipeline(x):\n    return compute(x)\n"
    )
    _git(tmp_path, "commit", "-q", "-am", "fix: compute off-by-one")
    return tmp_path


@pytest.fixture()
def project(repo: Path):
    name = "graphora-cli-test"
    assert main(["index", str(repo), "--project", name]) == 0
    yield name
    GraphStore(name).delete_graph()


def test_cli_index_and_stats(project, capsys):
    assert main(["stats", "--project", project]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["files"] == 2
    assert stats["functions"] >= 3


def test_cli_blast_json(project, capsys):
    assert main(["blast", "compute", "--project", project, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["symbols"][0]["name"] == "compute"
    assert any(c["name"] == "pipeline" for c in data["symbols"][0]["callers"])


def test_cli_review_facts_mode(project, repo: Path, tmp_path: Path, capsys):
    diff_file = tmp_path / "change.diff"
    diff_file.write_text(
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,2 +1,2 @@\n-def compute(x):\n+def compute(x, y=0):\n"
    )
    assert main(["review", "--diff", str(diff_file), "--project", project]) == 0
    out = capsys.readouterr().out
    assert "# Graphora Review" in out
    assert "compute" in out
    assert "Grounded facts" in out


def test_cli_risk_mine_and_top(project, repo: Path, capsys):
    assert main(["risk", "mine", str(repo), "--project", project]) == 0
    mined = json.loads(capsys.readouterr().out)
    assert mined["fix_commits"] == 1
    assert main(["risk", "top", "--project", project, "--json"]) == 0
    top = json.loads(capsys.readouterr().out)
    assert top[0]["name"] == "compute"


def test_cli_review_surfaces_risk_memory(project, repo: Path, tmp_path: Path, capsys):
    main(["risk", "mine", str(repo), "--project", project])
    capsys.readouterr()
    diff_file = tmp_path / "change2.diff"
    diff_file.write_text(
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,2 +1,2 @@\n-def compute(x):\n+def compute(x, y=0):\n"
    )
    main(["review", "--diff", str(diff_file), "--project", project])
    out = capsys.readouterr().out
    assert "risk-memory" in out
    assert "past fix/revert commits" in out


def test_cli_version_and_missing_diff(project, capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "graphora" in capsys.readouterr().out
