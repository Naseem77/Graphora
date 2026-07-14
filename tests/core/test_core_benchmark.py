"""Phase 6 tests: reproducible benchmark (integration, needs FalkorDB)."""

from pathlib import Path

import pytest

pytest.importorskip("falkordb")
from falkordb import FalkorDB  # noqa: E402

from graphora.benchmark import pick_symbols, run_benchmark  # noqa: E402
from graphora.indexer import index_repository  # noqa: E402


def _falkordb_available() -> bool:
    try:
        FalkorDB(host="localhost", port=6379).select_graph("graphora:ping").query("RETURN 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _falkordb_available(), reason="FalkorDB not running on localhost:6379")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    # A hub function with callers spread across realistic-size files.
    (tmp_path / "pkg" / "core.py").write_text("def hub(x):\n    return x\n")
    for i in range(5):
        padding = "\n".join(f"def helper_{i}_{j}():\n    return {j}\n" for j in range(20))
        (tmp_path / "pkg" / f"service_{i}.py").write_text(
            f"from pkg.core import hub\n\ndef caller_{i}(x):\n    return hub(x)\n\n{padding}"
        )
    for i in range(10):
        (tmp_path / "pkg" / f"filler_{i}.py").write_text(
            f"# filler module {i}\n" + "\n".join(f"def filler_{i}_{j}():\n    return {j}\n" for j in range(30))
        )
    (tmp_path / "tests" / "test_core.py").write_text(
        "from pkg.core import hub\n\ndef test_hub():\n    assert hub(1) == 1\n"
    )
    return tmp_path


@pytest.fixture()
def store(repo: Path):
    store = index_repository(repo, project="graphora-bench-test")
    yield store
    store.delete_graph()


def test_pick_symbols_returns_hub(store):
    assert pick_symbols(store, count=1) == ["hub"]


def test_benchmark_blast_far_cheaper_than_repo(store, repo: Path):
    result = run_benchmark(store, repo, symbols=["hub"])
    assert result.blast_tokens < result.repo_tokens / 10
    assert result.blast_tokens < result.changed_files_tokens
    assert result.saving_vs_repo > 90


def test_benchmark_counts_grounded_facts(store, repo: Path):
    result = run_benchmark(store, repo, symbols=["hub"])
    assert result.grounded_facts["callers"] == 5
    assert result.grounded_facts["tests"] == 1


def test_benchmark_is_deterministic(store, repo: Path):
    a = run_benchmark(store, repo, symbols=["hub"])
    b = run_benchmark(store, repo, symbols=["hub"])
    assert a == b


def test_benchmark_render_contains_table(store, repo: Path):
    text = run_benchmark(store, repo, symbols=["hub"]).render()
    assert "| Context strategy | Prompt tokens |" in text
    assert "Whole-repo dump" in text
    assert "reproducible" in text
