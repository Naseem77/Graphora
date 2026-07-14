"""Phase 3 tests: risk memory git miner (integration, needs FalkorDB + git)."""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("falkordb")
from falkordb import FalkorDB  # noqa: E402

from graphora.blast import blast_radius  # noqa: E402
from graphora.indexer import index_repository  # noqa: E402
from graphora.risk import compute_risk_score, mine_risk_memory, risk_report  # noqa: E402


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
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.dev")
    _git(repo, "config", "user.name", "tester")

    (repo / "pkg").mkdir()
    (repo / "pkg" / "billing.py").write_text(
        "def charge(amount):\n    return amount\n\ndef refund(amount):\n    return -amount\n"
    )
    (repo / "pkg" / "stable.py").write_text("def calm_function():\n    return 'never breaks'\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial commit")

    # Two fix commits touching charge(), one revert.
    (repo / "pkg" / "billing.py").write_text(
        "def charge(amount):\n    return amount * 1\n\ndef refund(amount):\n    return -amount\n"
    )
    _git(repo, "commit", "-q", "-am", "fix: charge rounding bug")

    (repo / "pkg" / "billing.py").write_text(
        "def charge(amount):\n    return int(amount)\n\ndef refund(amount):\n    return -amount\n"
    )
    _git(repo, "commit", "-q", "-am", "hotfix: charge crashes on float input")

    (repo / "pkg" / "billing.py").write_text(
        "def charge(amount):\n    return amount * 1\n\ndef refund(amount):\n    return -amount\n"
    )
    _git(repo, "commit", "-q", "-am", 'Revert "hotfix: charge crashes on float input"')

    # A feature commit that must NOT count.
    (repo / "pkg" / "stable.py").write_text(
        "def calm_function():\n    return 'never breaks'\n\ndef new_feature():\n    return 1\n"
    )
    _git(repo, "commit", "-q", "-am", "add shiny new feature")
    return repo


@pytest.fixture()
def store(git_repo: Path):
    store = index_repository(git_repo, project="graphora-risk-test")
    yield store
    store.delete_graph()


def test_miner_finds_fix_commits_and_annotates_symbols(git_repo: Path, store):
    stats = mine_risk_memory(store, git_repo)
    assert stats.fix_commits == 3  # 2 fixes + 1 revert, feature excluded
    rows = store.query("MATCH (s:Function {name: 'charge'}) RETURN s.fix_count, s.risk_score")
    fix_count, risk_score = rows[0]
    assert int(fix_count) == 3
    assert float(risk_score) > 0.5  # broke often and recently


def test_calm_function_has_no_risk(git_repo: Path, store):
    mine_risk_memory(store, git_repo)
    rows = store.query("MATCH (s:Function {name: 'calm_function'}) RETURN coalesce(s.fix_count, 0)")
    assert int(rows[0][0]) == 0


def test_miner_is_idempotent(git_repo: Path, store):
    mine_risk_memory(store, git_repo)
    first = store.query("MATCH (s:Function {name: 'charge'}) RETURN s.fix_count")[0][0]
    mine_risk_memory(store, git_repo)  # second run must not double count
    second = store.query("MATCH (s:Function {name: 'charge'}) RETURN s.fix_count")[0][0]
    assert int(first) == int(second) == 3


def test_revert_kind_recorded(git_repo: Path, store):
    mine_risk_memory(store, git_repo)
    rows = store.query("MATCH (c:FixCommit {kind: 'revert'}) RETURN count(c)")
    assert int(rows[0][0]) == 1


def test_risk_report_ranks_charge_first(git_repo: Path, store):
    mine_risk_memory(store, git_repo)
    report = risk_report(store)
    assert report[0]["name"] == "charge"
    assert report[0]["fix_count"] == 3


def test_blast_radius_surfaces_risk_memory(git_repo: Path, store):
    mine_risk_memory(store, git_repo)
    context = blast_radius(store, ["charge"]).to_context()
    assert "RISK MEMORY" in context
    assert "3 fix/revert commits" in context


def test_risk_score_decays_over_time():
    now = datetime.now(timezone.utc)
    recent = compute_risk_score(3, now.isoformat(), now=now)
    old = compute_risk_score(3, (now - timedelta(days=365)).isoformat(), now=now)
    assert recent > old > 0
    assert compute_risk_score(0, now.isoformat()) == 0.0
    assert compute_risk_score(5, "garbage-date") > 0  # falls back gracefully
