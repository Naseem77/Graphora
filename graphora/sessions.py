"""Agent-session ingestion: load AI-agent session histories into the graph.

A different data source for the same graph: instead of parsing code, this
reads the session stores that AI coding agents already maintain and writes
work-history nodes:

    (:Session {agent})-[:TOUCHED]->(:WorkFile)
    (:Session)-[:IN_REPO]->(:Repo)
    (:Session)-[:REFERENCES]->(:Ref {kind: pr|issue|commit})

Supported sources, one reader per agent, all emitting the same shape:

- copilot: GitHub Copilot CLI, SQLite at ~/.copilot/session-store.db
- claude:  Claude Code, JSONL files under ~/.claude/projects/
- codex:   Codex CLI, JSONL rollouts under ~/.codex/sessions/

Everything is deterministic and read-only on the sources: no LLM, no writes.
Sessions across terminals — and across agents — become connected the moment
they touch the same file, repo, or PR.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_COPILOT_DB = Path.home() / ".copilot" / "session-store.db"
DEFAULT_CLAUDE_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_CODEX_ROOT = Path.home() / ".codex" / "sessions"

# Back-compat alias (pre-multi-source name)
DEFAULT_DB = DEFAULT_COPILOT_DB

_EMPTY = {"sessions": [], "files": [], "refs": [], "last_turns": []}


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# --- copilot ---------------------------------------------------------------


def read_copilot(db_path: str | Path | None = None, days: int = 30) -> dict:
    """Read the GitHub Copilot CLI session store (SQLite)."""
    db = Path(db_path) if db_path else DEFAULT_COPILOT_DB
    if not db.exists():
        raise FileNotFoundError(f"Session store not found: {db}")
    uri = f"file:{db}?mode=ro"
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
            return dict(_EMPTY)
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
        return {"sessions": sessions, "files": [list(f) for f in files],
                "refs": [list(r) for r in refs], "last_turns": [list(t) for t in last_turns]}
    finally:
        con.close()


# --- claude code -----------------------------------------------------------


def read_claude_code(root: str | Path | None = None, days: int = 30) -> dict:
    """Read Claude Code session transcripts (JSONL under ~/.claude/projects/)."""
    base = Path(root) if root else DEFAULT_CLAUDE_ROOT
    if not base.exists():
        raise FileNotFoundError(f"Claude Code projects dir not found: {base}")
    cutoff = _cutoff(days)
    out = {"sessions": [], "files": [], "refs": [], "last_turns": []}
    for jsonl in sorted(base.glob("*/*.jsonl")):
        sid = jsonl.stem
        cwd = branch = summary = ""
        first_ts = last_ts = ""
        last_ask = ""
        touched: dict[str, str] = {}
        try:
            lines = jsonl.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            kind = obj.get("type")
            ts = obj.get("timestamp") or ""
            if ts:
                first_ts = first_ts or ts
                last_ts = max(last_ts, ts)
            if kind == "summary" and obj.get("summary"):
                summary = str(obj["summary"])[:120]
            elif kind == "last-prompt" and obj.get("lastPrompt") and not summary:
                summary = str(obj["lastPrompt"])[:120]
            elif kind == "user":
                cwd = cwd or obj.get("cwd", "")
                branch = branch or obj.get("gitBranch", "")
                content = obj.get("message", {}).get("content")
                if isinstance(content, str) and content and not content.startswith("<"):
                    last_ask = content[:300]
            elif kind == "assistant":
                content = obj.get("message", {}).get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        path = (block.get("input") or {}).get("file_path")
                        if path:
                            touched[str(path)] = str(block.get("name", "")).lower()
        if not last_ts or last_ts < cutoff:
            continue
        repo = Path(cwd).name if cwd else ""
        out["sessions"].append((sid, summary, cwd, repo, branch, first_ts, last_ts))
        out["files"].extend([sid, path, tool] for path, tool in touched.items())
        if last_ask:
            out["last_turns"].append([sid, last_ask])
    return out


# --- codex cli ---------------------------------------------------------------


def read_codex(root: str | Path | None = None, days: int = 30) -> dict:
    """Read Codex CLI rollouts (JSONL under ~/.codex/sessions/). Best-effort."""
    base = Path(root) if root else DEFAULT_CODEX_ROOT
    if not base.exists():
        raise FileNotFoundError(f"Codex sessions dir not found: {base}")
    cutoff = _cutoff(days)
    out = {"sessions": [], "files": [], "refs": [], "last_turns": []}
    for jsonl in sorted(base.rglob("*.jsonl")):
        sid = jsonl.stem
        cwd = summary = ""
        first_ts = last_ts = ""
        last_ask = ""
        try:
            lines = jsonl.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            ts = obj.get("timestamp") or ""
            if ts:
                first_ts = first_ts or ts
                last_ts = max(last_ts, ts)
            payload = obj.get("payload") or {}
            if obj.get("type") == "session_meta":
                sid = payload.get("id", sid)
                cwd = payload.get("cwd", "")
            elif payload.get("role") == "user":
                for item in payload.get("content") or []:
                    text = item.get("text", "") if isinstance(item, dict) else ""
                    if text and not text.startswith("<"):
                        last_ask = text[:300]
        if not last_ts or last_ts < cutoff:
            continue
        repo = Path(cwd).name if cwd else ""
        out["sessions"].append((sid, summary or last_ask[:120], cwd, repo, "", first_ts, last_ts))
        if last_ask:
            out["last_turns"].append([sid, last_ask])
    return out


SOURCES = {"copilot": read_copilot, "claude": read_claude_code, "codex": read_codex}


# --- ingestion ---------------------------------------------------------------


def _write(store, agent: str, data: dict) -> dict[str, int]:
    last_ask = dict(data["last_turns"])
    for sid, summary, cwd, repo, branch, created, updated in data["sessions"]:
        store.upsert_session(
            sid,
            {"agent": agent, "summary": summary, "cwd": cwd, "branch": branch,
             "created_at": created, "updated_at": updated, "last_ask": last_ask.get(sid, "")},
        )
        repo_name = repo or (Path(cwd).name if cwd else "")
        if repo_name:
            store.link_session_repo(sid, repo_name)
    for sid, path, tool in data["files"]:
        store.link_session_file(sid, path, tool)
    for sid, ref_type, ref_value in data["refs"]:
        store.link_session_ref(sid, ref_type, ref_value)
    return {
        "sessions": len(data["sessions"]),
        "files_touched": len(data["files"]),
        "refs": len(data["refs"]),
    }


def ingest_sources(
    store,
    sources: list[str] | None = None,
    days: int = 30,
    paths: dict[str, str | Path] | None = None,
) -> dict:
    """Ingest one or more agent session stores into `store`.

    `sources`: subset of {"copilot", "claude", "codex"} or None for all.
    `paths`: optional per-source location override, e.g. {"copilot": "/tmp/db"}.
    Missing sources are skipped (reported as {"skipped": reason}) when
    ingesting "all"; explicitly requested sources raise instead.
    """
    wanted = sources or list(SOURCES)
    unknown = set(wanted) - set(SOURCES)
    if unknown:
        raise ValueError(f"Unknown sources: {sorted(unknown)}. Known: {sorted(SOURCES)}")
    explicit = sources is not None
    paths = paths or {}
    results: dict[str, dict] = {}
    for name in wanted:
        reader = SOURCES[name]
        try:
            data = reader(paths.get(name), days=days)
        except FileNotFoundError as exc:
            if explicit:
                raise
            results[name] = {"skipped": str(exc)}
            continue
        results[name] = _write(store, name, data)
    if hasattr(store, "save"):
        store.save()
    return results


def ingest_session_store(store, db_path: str | Path | None = None, days: int = 30) -> dict[str, int]:
    """Back-compat: ingest only the Copilot CLI store. Returns counts."""
    counts = _write(store, "copilot", read_copilot(db_path, days=days))
    if hasattr(store, "save"):
        store.save()
    return counts


def connected(store, kind: str, value: str) -> list[dict]:
    """Sessions connected to a file path, repo, or ref value (e.g. a PR number)."""
    rows = store.sessions_connected(kind, value)
    return [
        {"session": r[0][:8], "agent": r[4] if len(r) > 4 else "",
         "summary": r[1], "updated_at": r[2], "via": r[3]}
        for r in rows
    ]
