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
