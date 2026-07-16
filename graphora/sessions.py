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
    """Ingest agent sessions into `store`. Returns node/edge counts."""
    db = Path(db_path) if db_path else DEFAULT_DB
    if not db.exists():
        raise FileNotFoundError(f"Session store not found: {db}")
    data = _read_source(db, days)
    last_ask = dict(data["last_turns"])

    for sid, summary, cwd, repo, branch, created, updated in data["sessions"]:
        store.query(
            """MERGE (s:Session {id: $id})
               SET s.summary = $summary, s.cwd = $cwd, s.branch = $branch,
                   s.created_at = $created, s.updated_at = $updated, s.last_ask = $ask""",
            {"id": sid, "summary": summary, "cwd": cwd, "branch": branch,
             "created": created, "updated": updated, "ask": last_ask.get(sid, "")},
        )
        repo_name = repo or (Path(cwd).name if cwd else "")
        if repo_name:
            store.query(
                """MERGE (r:Repo {name: $repo})
                   WITH r MATCH (s:Session {id: $id}) MERGE (s)-[:IN_REPO]->(r)""",
                {"repo": repo_name, "id": sid},
            )

    for sid, path, tool in data["files"]:
        store.query(
            """MERGE (f:WorkFile {path: $path})
               WITH f MATCH (s:Session {id: $id})
               MERGE (s)-[t:TOUCHED]->(f) SET t.tool = $tool""",
            {"path": path, "id": sid, "tool": tool},
        )

    for sid, ref_type, ref_value in data["refs"]:
        store.query(
            """MERGE (x:Ref {kind: $kind, value: $value})
               WITH x MATCH (s:Session {id: $id}) MERGE (s)-[:REFERENCES]->(x)""",
            {"kind": ref_type, "value": ref_value, "id": sid},
        )

    return {
        "sessions": len(data["sessions"]),
        "files_touched": len(data["files"]),
        "refs": len(data["refs"]),
    }


def connected(store, kind: str, value: str) -> list[dict]:
    """Sessions connected to a file path, repo, or ref value (e.g. a PR number)."""
    if kind == "file":
        cypher = """MATCH (s:Session)-[:TOUCHED]->(f:WorkFile)
                    WHERE f.path ENDS WITH $v
                    RETURN s.id, s.summary, s.updated_at, f.path ORDER BY s.updated_at DESC"""
    elif kind == "repo":
        cypher = """MATCH (s:Session)-[:IN_REPO]->(r:Repo {name: $v})
                    RETURN s.id, s.summary, s.updated_at, r.name ORDER BY s.updated_at DESC"""
    else:  # ref: pr / issue / commit value
        cypher = """MATCH (s:Session)-[:REFERENCES]->(x:Ref)
                    WHERE x.value = $v OR x.value ENDS WITH $v
                    RETURN s.id, s.summary, s.updated_at, x.kind + ' ' + x.value
                    ORDER BY s.updated_at DESC"""
    rows = store.query(cypher, {"v": value})
    return [
        {"session": r[0][:8], "summary": r[1], "updated_at": r[2], "via": r[3]}
        for r in rows
    ]
