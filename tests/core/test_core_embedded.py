"""Embedded backend tests: zero Docker, JSON persistence, parity with FalkorDB."""

import json
import subprocess
from pathlib import Path

import pytest

from graphora.blast import blast_radius
from graphora.embedded import EmbeddedGraphStore
from graphora.indexer import index_repository, update_files
from graphora.store import GraphStore, falkordb_available, open_store


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "core.py").write_text(
        "def compute(x):\n    return x + 1\n\ndef pipeline(x):\n    return compute(x)\n"
    )
    (repo / "pkg" / "api.py").write_text(
        "from pkg.core import compute\n\ndef handler(req):\n    return compute(req)\n"
    )
    (repo / "tests" / "test_core.py").write_text(
        "from pkg.core import compute\n\ndef test_compute():\n    assert compute(1) == 2\n"
    )
    return repo


@pytest.fixture()
def store(sample_repo: Path, tmp_path: Path) -> EmbeddedGraphStore:
    store = EmbeddedGraphStore("embedded-test", data_dir=tmp_path / "data")
    index_repository(sample_repo, project="embedded-test", store=store)
    return store


def test_embedded_index_builds_nodes_and_edges(store: EmbeddedGraphStore):
    stats = store.stats()
    assert stats["files"] == 3
    assert stats["functions"] == 4
    assert stats["calls"] >= 2
    assert store.has_files()


def test_embedded_persists_across_instances(store: EmbeddedGraphStore, tmp_path: Path):
    reopened = EmbeddedGraphStore("embedded-test", data_dir=tmp_path / "data")
    assert reopened.stats() == store.stats()
    assert reopened.find_symbol("compute")[0]["path"] == "pkg/core.py"


def test_embedded_blast_radius_matches_semantics(store: EmbeddedGraphStore):
    radius = blast_radius(store, ["compute"])
    impact = radius.symbols[0]
    caller_names = {c[0] for c in impact.callers}
    assert caller_names == {"pipeline", "handler"}          # tests excluded
    assert {t[0] for t in impact.tests} == {"test_compute"}
    confidences = {c[2] for c in impact.callers}
    assert "EXTRACTED" in confidences                        # same-file call
    assert "pkg/api.py" in impact.importers


def test_embedded_incremental_update_and_delete(sample_repo: Path, store: EmbeddedGraphStore):
    (sample_repo / "pkg" / "extra.py").write_text("def extra():\n    return 0\n")
    update_files(sample_repo, ["pkg/extra.py"], store=store)
    assert store.stats()["files"] == 4
    (sample_repo / "pkg" / "extra.py").unlink()
    update_files(sample_repo, ["pkg/extra.py"], store=store)
    assert store.stats()["files"] == 3
    assert not store.find_symbol("extra")


def test_open_store_explicit_embedded(tmp_path: Path):
    store = open_store("x", backend="embedded", data_dir=str(tmp_path))
    assert isinstance(store, EmbeddedGraphStore)
    assert store.backend == "embedded"


def test_open_store_auto_falls_back_when_unreachable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GRAPHORA_DATA_DIR", str(tmp_path))
    # port 1 is never a FalkorDB
    store = open_store("x", backend="auto", host="localhost", port=1)
    assert isinstance(store, EmbeddedGraphStore)


def test_open_store_rejects_unknown_backend():
    with pytest.raises(ValueError):
        open_store("x", backend="bogus")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_embedded_risk_memory_end_to_end(tmp_path: Path):
    from graphora.risk import mine_risk_memory, risk_report

    repo = tmp_path / "riskrepo"
    (repo / "pkg").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "pkg" / "billing.py").write_text("def charge(amount):\n    return amount\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: add billing")
    (repo / "pkg" / "billing.py").write_text("def charge(amount):\n    return amount * 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: charge rounding bug")

    store = EmbeddedGraphStore("risk-embedded", data_dir=tmp_path / "data")
    index_repository(repo, project="risk-embedded", store=store)
    stats = mine_risk_memory(store, repo)
    assert stats.fix_commits == 1
    report = risk_report(store)
    assert report and report[0]["name"] == "charge"
    assert report[0]["risk_score"] > 0

    # idempotent
    again = mine_risk_memory(store, repo)
    assert again.fix_commits == 0
    assert risk_report(store)[0]["fix_count"] == report[0]["fix_count"]

    # risk surfaces in blast radius
    radius = blast_radius(store, ["charge"])
    assert radius.symbols[0].fix_count == 1


def test_embedded_benchmark_is_deterministic(sample_repo: Path, store: EmbeddedGraphStore):
    from graphora.benchmark import run_benchmark

    a = run_benchmark(store, sample_repo, symbols=["compute"])
    b = run_benchmark(store, sample_repo, symbols=["compute"])
    assert a.render() == b.render()
    # on a 3-file toy repo savings don't apply; verify grounded facts instead
    assert a.grounded_facts["callers"] == 2
    assert a.grounded_facts["tests"] == 1


def test_embedded_cli_end_to_end(sample_repo: Path, tmp_path: Path, monkeypatch, capsys):
    from graphora.cli import main

    monkeypatch.setenv("GRAPHORA_DATA_DIR", str(tmp_path / "clidata"))
    assert main(["index", str(sample_repo), "--project", "cli-embedded", "--backend", "embedded"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["backend"] == "embedded"
    assert out["files"] == 3

    assert main(["blast", "compute", "--project", "cli-embedded", "--backend", "embedded", "--json"]) == 0
    blast_out = json.loads(capsys.readouterr().out)
    assert blast_out["symbols"][0]["name"] == "compute"


@pytest.mark.skipif(not falkordb_available(), reason="FalkorDB not running on localhost:6379")
def test_embedded_matches_falkordb_output(sample_repo: Path, tmp_path: Path):
    """Parity: identical stats and blast-radius context on both backends."""
    embedded = EmbeddedGraphStore("parity-e", data_dir=tmp_path / "data")
    index_repository(sample_repo, project="parity-e", store=embedded)
    falkor = GraphStore("parity-f")
    try:
        index_repository(sample_repo, project="parity-f", store=falkor)
        e_stats, f_stats = embedded.stats(), falkor.stats()
        for key in ("files", "functions", "classes", "modules", "calls"):
            assert e_stats[key] == f_stats[key], key
        e_ctx = blast_radius(embedded, ["compute"]).to_context()
        f_ctx = blast_radius(falkor, ["compute"]).to_context()
        assert e_ctx == f_ctx
    finally:
        falkor.delete_graph()
