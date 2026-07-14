"""Phase 2 tests: deterministic blast radius (integration, needs FalkorDB)."""

from pathlib import Path

import pytest

pytest.importorskip("falkordb")
from falkordb import FalkorDB  # noqa: E402

from graphora.blast import blast_radius, blast_radius_for_diff  # noqa: E402
from graphora.indexer import index_repository  # noqa: E402


def _falkordb_available() -> bool:
    try:
        FalkorDB(host="localhost", port=6379).select_graph("graphora:ping").query("RETURN 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _falkordb_available(), reason="FalkorDB not running on localhost:6379")


@pytest.fixture()
def store(tmp_path: Path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "core.py").write_text(
        "def compute(x):\n"
        "    return x + 1\n\n"
        "def pipeline(x):\n"
        "    return compute(x)\n\n"
        "def unrelated():\n"
        "    return 42\n"
    )
    (tmp_path / "pkg" / "api.py").write_text(
        "from pkg.core import compute\n\ndef handler(req):\n    return compute(req)\n"
    )
    (tmp_path / "tests" / "test_core.py").write_text(
        "from pkg.core import compute\n\ndef test_compute():\n    assert compute(1) == 2\n"
    )
    store = index_repository(tmp_path, project="graphora-blast-test")
    yield store
    store.delete_graph()


def test_blast_radius_finds_callers_and_tests(store):
    radius = blast_radius(store, ["compute"])
    assert len(radius.symbols) == 1
    impact = radius.symbols[0]
    caller_names = {name for name, _, _ in impact.callers}
    assert caller_names == {"pipeline", "handler"}  # test callers excluded
    test_names = {name for name, _, _ in impact.tests}
    assert test_names == {"test_compute"}


def test_blast_radius_confidence_tags(store):
    impact = blast_radius(store, ["compute"]).symbols[0]
    confidences = {name: conf for name, _, conf in impact.callers}
    assert confidences["pipeline"] == "EXTRACTED"  # same file
    assert confidences["handler"] == "INFERRED"  # cross file


def test_blast_radius_unresolved_symbol(store):
    radius = blast_radius(store, ["does_not_exist"])
    assert radius.unresolved == ["does_not_exist"]
    assert radius.symbols == []


def test_blast_radius_for_diff(store):
    diff = """\
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,2 +1,2 @@
-def compute(x):
+def compute(x, offset=0):
"""
    radius = blast_radius_for_diff(store, diff)
    assert radius.changed_files == ["pkg/core.py"]
    assert any(s.name == "compute" for s in radius.symbols)
    context = radius.to_context()
    assert "callers (2)" in context
    assert "tests (1)" in context


def test_context_is_compact(store):
    context = blast_radius(store, ["compute", "pipeline", "unrelated"]).to_context()
    # The whole point: context stays tiny compared to a repo dump.
    assert len(context) < 2000
    assert "unrelated" in context


def test_to_dict_roundtrip(store):
    data = blast_radius(store, ["compute"]).to_dict()
    assert data["symbols"][0]["name"] == "compute"
    assert {c["confidence"] for c in data["symbols"][0]["callers"]} <= {"EXTRACTED", "INFERRED", "AMBIGUOUS"}
