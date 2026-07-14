"""Phase 1 tests: FalkorDB store + indexer (integration, needs FalkorDB on :6379)."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("falkordb")
from falkordb import FalkorDB  # noqa: E402

from graphora.indexer import collect_source_files, index_repository, update_files  # noqa: E402
from graphora.store import GraphStore  # noqa: E402


def _falkordb_available() -> bool:
    try:
        FalkorDB(host="localhost", port=6379).select_graph("graphora:ping").query("RETURN 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _falkordb_available(), reason="FalkorDB not running on localhost:6379")


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def compute(x):\n    return x + 1\n\ndef pipeline(x):\n    return compute(x)\n"
    )
    (tmp_path / "pkg" / "api.py").write_text(
        "from pkg.core import compute\n\ndef handler(req):\n    return compute(req)\n"
    )
    (tmp_path / "tests" / "test_core.py").write_text(
        "from pkg.core import compute\n\ndef test_compute():\n    assert compute(1) == 2\n"
    )
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("def ignored(): pass\n")
    return tmp_path


@pytest.fixture()
def store(sample_repo: Path):
    store = index_repository(sample_repo, project="graphora-test")
    yield store
    store.delete_graph()


def test_collect_skips_ignored_dirs(sample_repo: Path):
    files = [str(p) for p in collect_source_files(sample_repo)]
    assert not any("node_modules" in f for f in files)
    assert len(files) == 3


def test_index_builds_nodes_and_edges(store: GraphStore):
    stats = store.stats()
    assert stats["files"] == 3
    assert stats["functions"] >= 4  # compute, pipeline, handler, test_compute
    assert stats["calls"] >= 3


def test_cross_file_call_confidence_is_inferred(store: GraphStore):
    rows = store.query(
        """
        MATCH (a:Function {name: 'handler'})-[r:CALLS]->(b:Function {name: 'compute'})
        RETURN r.confidence
        """
    )
    assert rows and rows[0][0] == "INFERRED"


def test_same_file_call_confidence_is_extracted(store: GraphStore):
    rows = store.query(
        """
        MATCH (a:Function {name: 'pipeline'})-[r:CALLS]->(b:Function {name: 'compute'})
        RETURN r.confidence
        """
    )
    assert rows and rows[0][0] == "EXTRACTED"


def test_test_files_flagged(store: GraphStore):
    rows = store.query("MATCH (f:File {is_test: true}) RETURN f.path")
    assert ["tests/test_core.py"] in rows


def test_incremental_update_and_delete(sample_repo: Path, store: GraphStore):
    (sample_repo / "pkg" / "extra.py").write_text("def brand_new():\n    return 1\n")
    update_files(sample_repo, ["pkg/extra.py"], project="graphora-test", store=store)
    rows = store.query("MATCH (f:Function {name: 'brand_new'}) RETURN f.path")
    assert rows

    (sample_repo / "pkg" / "extra.py").unlink()
    update_files(sample_repo, ["pkg/extra.py"], project="graphora-test", store=store)
    rows = store.query("MATCH (f:File {path: 'pkg/extra.py'}) RETURN f")
    assert not rows
