"""Session-store ingestion tests: embedded always, FalkorDB when available on :6379."""

import sqlite3
from pathlib import Path

import pytest

from graphora.embedded import EmbeddedGraphStore
from graphora.sessions import connected, ingest_session_store


def _falkordb_available() -> bool:
    try:
        from falkordb import FalkorDB

        FalkorDB(host="localhost", port=6379).select_graph("graphora:ping").query("RETURN 1")
        return True
    except Exception:
        return False


FALKORDB_UP = _falkordb_available()


@pytest.fixture()
def session_db(tmp_path: Path) -> Path:
    db = tmp_path / "session-store.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, repository TEXT,
            host_type TEXT, branch TEXT, summary TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
            turn_index INTEGER, user_message TEXT, assistant_response TEXT,
            timestamp TEXT DEFAULT (datetime('now')));
        CREATE TABLE session_files (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, file_path TEXT, tool_name TEXT, turn_index INTEGER,
            first_seen_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE session_refs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ref_type TEXT, ref_value TEXT, turn_index INTEGER,
            created_at TEXT DEFAULT (datetime('now')));

        INSERT INTO sessions (id, cwd, repository, branch, summary) VALUES
          ('aaa11111-1111', '/home/u/proj', 'org/proj', 'main', 'Fix build'),
          ('bbb22222-2222', '/home/u/proj', 'org/proj', 'main', 'Review PR'),
          ('ccc33333-3333', '/home/u/other', 'org/other', '', 'Other work');
        INSERT INTO turns (session_id, turn_index, user_message) VALUES
          ('aaa11111-1111', 0, 'fix the docker build'),
          ('aaa11111-1111', 1, 'add a healthcheck'),
          ('bbb22222-2222', 0, 'review my pr');
        INSERT INTO session_files (session_id, file_path, tool_name) VALUES
          ('aaa11111-1111', '/home/u/proj/build.yml', 'edit'),
          ('bbb22222-2222', '/home/u/proj/build.yml', 'view'),
          ('ccc33333-3333', '/home/u/other/main.py', 'edit');
        INSERT INTO session_refs (session_id, ref_type, ref_value) VALUES
          ('aaa11111-1111', 'pr', '275'),
          ('bbb22222-2222', 'pr', '275');
        """
    )
    con.commit()
    con.close()
    return db


@pytest.fixture(params=["embedded", "falkordb"])
def store(request, tmp_path):
    if request.param == "embedded":
        store = EmbeddedGraphStore("sessions-test", data_dir=tmp_path / "graphs")
        yield store
        store.delete_graph()
    else:
        if not FALKORDB_UP:
            pytest.skip("FalkorDB not running on localhost:6379")
        from graphora.store import GraphStore

        store = GraphStore("sessions-test")
        yield store
        store.delete_graph()


def test_ingest_counts(store, session_db):
    counts = ingest_session_store(store, db_path=session_db, days=7)
    assert counts == {"sessions": 3, "files_touched": 3, "refs": 2}


def test_ingest_is_idempotent(store, session_db):
    ingest_session_store(store, db_path=session_db, days=7)
    ingest_session_store(store, db_path=session_db, days=7)
    # re-ingesting must not duplicate sessions or TOUCHED edges
    assert len(connected(store, "file", "build.yml")) == 2
    assert len(connected(store, "repo", "org/proj")) == 2
    assert len(connected(store, "ref", "275")) == 2


def test_session_node_has_last_ask(store, session_db):
    ingest_session_store(store, db_path=session_db, days=7)
    if hasattr(store, "_data"):  # embedded
        ask = store._data["sessions"]["aaa11111-1111"]["last_ask"]
    else:  # falkordb
        ask = store.query("MATCH (s:Session {id: 'aaa11111-1111'}) RETURN s.last_ask")[0][0]
    assert ask == "add a healthcheck"


def test_connected_by_file(store, session_db):
    ingest_session_store(store, db_path=session_db, days=7)
    hits = connected(store, "file", "build.yml")
    ids = {h["session"] for h in hits}
    assert ids == {"aaa11111", "bbb22222"}


def test_connected_by_ref(store, session_db):
    ingest_session_store(store, db_path=session_db, days=7)
    hits = connected(store, "ref", "275")
    assert len(hits) == 2


def test_connected_by_repo(store, session_db):
    ingest_session_store(store, db_path=session_db, days=7)
    hits = connected(store, "repo", "org/other")
    assert [h["session"] for h in hits] == ["ccc33333"]


def test_missing_db_raises(store, tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_session_store(store, db_path=tmp_path / "nope.db")


@pytest.fixture()
def claude_root(tmp_path: Path) -> Path:
    import json as _json

    proj = tmp_path / "claude-projects" / "-Users-u-proj"
    proj.mkdir(parents=True)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    lines = [
        {"type": "last-prompt", "lastPrompt": "fix the flaky test", "sessionId": "cl-1"},
        {"type": "user", "cwd": "/home/u/proj", "gitBranch": "main", "timestamp": now,
         "message": {"content": "fix the flaky test"}},
        {"type": "assistant", "timestamp": now, "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/home/u/proj/build.yml"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/home/u/proj/main.py"}},
        ]}},
        {"type": "user", "cwd": "/home/u/proj", "timestamp": now,
         "message": {"content": "also add a retry"}},
    ]
    (proj / "claude-session-1.jsonl").write_text(
        "\n".join(_json.dumps(o) for o in lines), encoding="utf-8"
    )
    return tmp_path / "claude-projects"


def test_read_claude_code(claude_root):
    from graphora.sessions import read_claude_code

    data = read_claude_code(claude_root, days=7)
    assert len(data["sessions"]) == 1
    sid, summary, cwd, repo, branch, _, _ = data["sessions"][0]
    assert sid == "claude-session-1"
    assert summary == "fix the flaky test"
    assert (cwd, repo, branch) == ("/home/u/proj", "proj", "main")
    assert sorted(p for _, p, _ in data["files"]) == ["/home/u/proj/build.yml", "/home/u/proj/main.py"]
    assert data["last_turns"] == [["claude-session-1", "also add a retry"]]


def test_read_claude_code_skips_old_sessions(claude_root, tmp_path):
    import json as _json

    old = claude_root / "-Users-u-old"
    old.mkdir()
    (old / "ancient.jsonl").write_text(_json.dumps(
        {"type": "user", "cwd": "/home/u/old", "timestamp": "2020-01-01T00:00:00Z",
         "message": {"content": "old stuff"}}), encoding="utf-8")
    from graphora.sessions import read_claude_code

    data = read_claude_code(claude_root, days=7)
    assert [s[0] for s in data["sessions"]] == ["claude-session-1"]


def test_read_codex(tmp_path):
    import json as _json
    from datetime import datetime, timezone

    root = tmp_path / "codex-sessions" / "2026" / "07"
    root.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        {"type": "session_meta", "timestamp": now, "payload": {"id": "cx-1", "cwd": "/home/u/proj"}},
        {"type": "response_item", "timestamp": now,
         "payload": {"role": "user", "content": [{"type": "input_text", "text": "refactor the parser"}]}},
    ]
    (root / "rollout-1.jsonl").write_text("\n".join(_json.dumps(o) for o in lines), encoding="utf-8")
    from graphora.sessions import read_codex

    data = read_codex(tmp_path / "codex-sessions", days=7)
    assert [s[0] for s in data["sessions"]] == ["cx-1"]
    assert data["sessions"][0][2] == "/home/u/proj"
    assert data["last_turns"] == [["cx-1", "refactor the parser"]]


def test_ingest_sources_cross_agent(store, session_db, claude_root):
    from graphora.sessions import ingest_sources

    results = ingest_sources(
        store, sources=["copilot", "claude"], days=7,
        paths={"copilot": session_db, "claude": claude_root},
    )
    assert results["copilot"]["sessions"] == 3
    assert results["claude"]["sessions"] == 1
    # cross-agent memory: both agents touched build.yml
    hits = connected(store, "file", "build.yml")
    agents = {h["agent"] for h in hits}
    assert agents == {"copilot", "claude"}


def test_ingest_sources_skips_missing_when_all(store, session_db, tmp_path):
    from graphora.sessions import ingest_sources

    results = ingest_sources(
        store, sources=None, days=7,
        paths={"copilot": session_db,
               "claude": tmp_path / "nope-claude",
               "codex": tmp_path / "nope-codex"},
    )
    assert results["copilot"]["sessions"] == 3
    assert "skipped" in results["claude"]
    assert "skipped" in results["codex"]


def test_ingest_sources_unknown_source_raises(store):
    from graphora.sessions import ingest_sources

    with pytest.raises(ValueError):
        ingest_sources(store, sources=["gemini"])


def test_connected_reports_agent(store, session_db):
    ingest_session_store(store, db_path=session_db, days=7)
    hits = connected(store, "file", "build.yml")
    assert all(h["agent"] == "copilot" for h in hits)
