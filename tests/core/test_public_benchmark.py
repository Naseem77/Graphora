"""Public benchmark script tests: offline, uses local scripted repos."""

import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import public_benchmark  # noqa: E402


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "toy"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text(
        "def compute(x):\n    return x + 1\n\ndef pipeline(x):\n    return compute(x)\n"
        "\ndef handler(x):\n    return compute(x)\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    return repo


def test_bench_one_produces_consistent_row(tmp_path: Path):
    repo = _make_repo(tmp_path)
    row1 = public_benchmark.bench_one(repo, tmp_path / "d1")
    row2 = public_benchmark.bench_one(repo, tmp_path / "d2")
    assert row1 == row2  # deterministic
    assert row1["repo"] == "toy"
    assert row1["files"] == 1
    assert row1["symbols"] == ["compute"]
    assert row1["blast_tokens"] > 0
    assert 0 <= row1["saving_vs_repo"] <= 100 or row1["saving_vs_repo"] < 0  # percentage units


def test_render_formats_markdown_table(tmp_path: Path):
    repo = _make_repo(tmp_path)
    row = public_benchmark.bench_one(repo, tmp_path / "d")
    report = public_benchmark.render([row], {"toy": "abcdef1234567890"})
    assert "| toy | `abcdef1234` |" in report
    assert "Reproduce:" in report
    assert report.count("|") > 10


def test_repos_are_pinned():
    for url, sha in public_benchmark.REPOS:
        assert url.startswith("https://github.com/")
        assert sha and len(sha) == 40, f"{url} must pin a full 40-char commit SHA"
