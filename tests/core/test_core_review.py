"""Review: facts mode, the optional LLM pass (with a fake model), and diff edge cases."""

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from graphora.embedded import EmbeddedGraphStore
from graphora.impact import changed_symbols, what_could_break_for_diff
from graphora.indexer import index_repository
from graphora.review import _parse_llm_review, review_diff


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "core.py").write_text(
        "def compute(x):\n    return x + 1\n\n"
        "def untested(x):\n    return x\n\n"
        "def pipeline(x):\n    return compute(x) + untested(x)\n"
    )
    (repo / "tests" / "test_core.py").write_text(
        "from pkg.core import compute\n\ndef test_compute():\n    assert compute(1) == 2\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.dev")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture()
def store(repo: Path, tmp_path: Path) -> EmbeddedGraphStore:
    store = EmbeddedGraphStore("review", data_dir=tmp_path / "data")
    index_repository(repo, project="review", store=store)
    return store


BODY_EDIT = (
    "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -4,2 +4,2 @@\n"
    " def untested(x):\n-    return x\n+    return x * 2\n"
)


def test_facts_review_warns_on_untested_changed_code(store):
    review = review_diff(store, BODY_EDIT, use_llm=False)
    text = review.render()
    assert "Changed: `untested`" in review.summary
    assert any(f.severity == "warning" and "`untested`" in f.body for f in review.findings)
    assert "_Facts mode: no model was called._" in text
    assert "Context sent to" not in text


def test_facts_review_does_not_warn_when_a_test_reaches_it(store):
    diff = "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1,2 +1,2 @@\n def compute(x):\n-    return x + 1\n+    return x + 2\n"
    review = review_diff(store, diff, use_llm=False)
    assert not review.findings


def test_llm_review_uses_the_model_answer(store, monkeypatch):
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        content = json.dumps(
            {
                "summary": "Doubles untested().",
                "findings": [
                    {"path": "pkg/core.py", "line": 5, "severity": "warning", "body": "pipeline result changes"},
                    {"path": "", "body": "dropped: no path"},
                    "not a dict",
                ],
            }
        )
        message = types.SimpleNamespace(content=f"Sure!\n{content}\n")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=completion))
    review = review_diff(store, BODY_EDIT, model="fake/model", use_llm=True)
    assert review.summary == "Doubles untested()."
    bodies = [f.body for f in review.findings]
    assert "pipeline result changes" in bodies
    assert not any("dropped" in b for b in bodies)
    assert "What could break if you change `untested`" in seen["messages"][0]["content"]
    assert "Context sent to fake/model" in review.render()


def test_parse_llm_review_tolerates_garbage():
    assert _parse_llm_review("no json here") == ("no json here", [])
    assert _parse_llm_review("{not json}") == ("{not json}", [])
    summary, findings = _parse_llm_review('{"summary": "ok", "findings": [{"path": "a.py", "line": "x"}]}')
    assert summary == "ok" and findings[0].line is None


def test_diff_deleting_a_function_finds_it_by_name(store):
    # Graph built from the old code; the diff removes `untested` entirely.
    diff = (
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -4,3 +4,0 @@\n"
        "-def untested(x):\n-    return x\n-\n"
    )
    targets, unresolved = changed_symbols(store, diff)
    assert "untested" in {t["qualname"] for t in targets}
    assert not unresolved
    impact = what_could_break_for_diff(store, diff)
    assert "pipeline" in {b.caller["qualname"] for b in impact.will_break}


def test_diff_new_and_deleted_files(store):
    new_file = "--- /dev/null\n+++ b/pkg/extra.py\n@@ -0,0 +1,2 @@\n+def extra():\n+    return 1\n"
    targets, unresolved = changed_symbols(store, new_file)
    assert targets == []  # not indexed yet
    assert unresolved == []
    deleted = "--- a/pkg/core.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def compute(x):\n-    return x + 1\n"
    targets, _ = changed_symbols(store, deleted)
    assert "compute" in {t["qualname"] for t in targets}


def test_cli_breaks_and_review_from_git_range_and_stdin(repo, store, tmp_path, capsys, monkeypatch):
    import io

    from graphora.cli import main

    monkeypatch.setenv("GRAPHORA_DATA_DIR", str(tmp_path / "data"))
    (repo / "pkg" / "core.py").write_text((repo / "pkg" / "core.py").read_text().replace("return x\n", "return x * 2\n"))
    _git(repo, "commit", "-q", "-am", "double")
    common = ["--project", "review", "--backend", "embedded"]

    assert main(["breaks", "--git-range", "HEAD~1..HEAD", "--repo", str(repo), *common]) == 0
    assert "What could break if you change `untested`" in capsys.readouterr().out

    assert main(["review", "--git-range", "HEAD~1..HEAD", "--repo", str(repo), *common]) == 0
    assert "`untested` has 1 caller(s) but no test" in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO(BODY_EDIT))
    assert main(["breaks", *common]) == 0
    assert "`untested`" in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["breaks", *common]) == 1
    assert "Give symbols, or a diff" in capsys.readouterr().err
