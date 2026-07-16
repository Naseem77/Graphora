"""Agent-session ingestion: load a Copilot CLI session store into the graph.

A different data source for the same graph: instead of parsing code, this
reads the SQLite database the Copilot CLI already maintains
(`~/.copilot/session-store.db`) and writes work-history nodes:

    (:Session)-[:TOUCHED]->(:WorkFile)
    (:Session)-[:IN_REPO]->(:Repo)
    (:Session)-[:REFERENCES]->(:Ref {kind: pr|issue|commit})

Everything is deterministic and read-only on the source: no LLM, no writes
to the session store. Sessions across terminals become connected the moment
they touch the same file, repo, or PR.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB = Path.home() / ".copilot" / "session-store.db"


def _read_source(db_path: Path, days: int) -> dict[str, list[tuple]]:
    uri = f"file:{db_path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        cutoff = f"-{int(days)} days"
        sessions = con.execute(
            """SELECT id, COALESCE(summary,''), COALESCE(cwd,''), COALESCE(repository,''),
                      COALESCE(branch,''), created_at, updated_at
               FROM sessions WHERE updated_at > datetime('now', ?)""",
            (cutoff,),
        ).fetchall()
        ids = [row[0] for row in sessions]
        if not ids:
            return {"sessions": [], "files": [], "refs": [], "last_turns": []}
        marks = ",".join("?" * len(ids))
        files = con.execute(
            f"""SELECT session_id, file_path, COALESCE(tool_name,'')
                FROM session_files WHERE session_id IN ({marks})""",
            ids,
        ).fetchall()
        refs = con.execute(
            f"""SELECT session_id, ref_type, ref_value
                FROM session_refs WHERE session_id IN ({marks})""",
            ids,
        ).fetchall()
        last_turns = con.execute(
            f"""SELECT t.session_id, substr(COALESCE(t.user_message,''),1,300)
                FROM turns t
                JOIN (SELECT session_id, MAX(turn_index) AS mi FROM turns
                      WHERE user_message IS NOT NULL GROUP BY session_id) m
                  ON m.session_id = t.session_id AND m.mi = t.turn_index
                WHERE t.session_id IN ({marks})""",
            ids,
        ).fetchall()
        return {"sessions": sessions, "files": files, "refs": refs, "last_turns": last_turns}
    finally:
        con.close()


def ingest_session_store(store, db_path: str | Path | None = None, days: int = 30) -> dict[str, int]:
    """Ingest agent sessions into `store` (FalkorDB or embedded). Returns counts."""
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        raise FileNotFoundError(f"Session store not found: {db}")
    data = _read_source(db, days)
    last_ask = dict(data["last_turns"])

    for sid, summary, cwd, repo, branch, created, updated in data["sessions"]:
        store.upsert_session(
            sid,
            {"summary": summary, "cwd": cwd, "branch": branch,
             "created_at": created, "updated_at": updated, "last_ask": last_ask.get(sid, "")},
        )
        repo_name = repo or (Path(cwd).name if cwd else "")
        if repo_name:
            store.link_session_repo(sid, repo_name)

    for sid, path, tool in data["files"]:
        store.link_session_file(sid, path, tool)

    for sid, ref_type, ref_value in data["refs"]:
        store.link_session_ref(sid, ref_type, ref_value)

    if hasattr(store, "save"):
        store.save()

    return {
        "sessions": len(data["sessions"]),
        "files_touched": len(data["files"]),
        "refs": len(data["refs"]),
    }


def connected(store, kind: str, value: str) -> list[dict]:
    """Sessions connected to a file path, repo, or ref value (e.g. a PR number)."""
    rows = store.sessions_connected(kind, value)
    return [
        {"session": r[0][:8], "summary": r[1], "updated_at": r[2], "via": r[3]}
        for r in rows
    ]
