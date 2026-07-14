"""Risk memory: the graph learns where the codebase breaks.

Deterministically mines git history (zero LLM) for fix/revert/hotfix
commits, attributes them to the functions and classes they touched, and
annotates the graph:

- (:FixCommit {sha, date, subject, kind}) nodes
- (:FixCommit)-[:TOUCHED]->(:File) and (:FixCommit)-[:FIXED]->(:Function|:Class)
- per-symbol properties: fix_count, last_broke_at, risk_score

risk_score = (1 - 0.7^fix_count) * 0.5^(months_since_last_fix / 6)

So a symbol fixed often and recently scores near 1.0, and the score decays
as the area stays quiet. Reviews and blast-radius output surface it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from graphora.parser import _extract_symbols  # deterministic symbol regexes
from graphora.parser import extract_symbols_from_diff
from graphora.store import GraphStore

FIX_GREP = r"\b(fix(e[sd])?|hotfix|bugfix|bug|regression|revert(s|ed)?|patch(e[sd])?)\b"
_FIX_RE = re.compile(FIX_GREP, re.IGNORECASE)
_REVERT_RE = re.compile(r"^revert\b|\brevert(s|ed)?\b", re.IGNORECASE)
_HUNK_HEADER_RE = re.compile(r"^@@ [^@]+ @@ ?(.*)$")
_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")


@dataclass(frozen=True)
class RiskStats:
    commits_scanned: int
    fix_commits: int
    symbols_annotated: int
    files_touched: int


def mine_risk_memory(
    store: GraphStore,
    repo_root: str | Path,
    since: str | None = None,
    max_commits: int = 2000,
) -> RiskStats:
    """Mine fix/revert history from git into the graph. Idempotent per commit."""
    repo_root = Path(repo_root).resolve()
    commits = _list_fix_commits(repo_root, since, max_commits)
    known = store.known_fix_shas()

    symbols_annotated = 0
    files_touched = 0
    for sha, date, subject in commits:
        if sha in known:
            continue
        kind = "revert" if _REVERT_RE.search(subject) else "fix"
        store.upsert_fix_commit(sha, date, subject, kind)
        diff = _commit_diff(repo_root, sha)
        for path, symbol_names in _attribute_diff(diff).items():
            files_touched += store.touch_file(sha, path)
            for name in symbol_names:
                symbols_annotated += store.record_symbol_fix(path, name, sha, date)

    _recompute_risk_scores(store)
    return RiskStats(
        commits_scanned=len(commits),
        fix_commits=len([c for c in commits if c[0] not in known]),
        symbols_annotated=symbols_annotated,
        files_touched=files_touched,
    )


def risk_report(store: GraphStore, limit: int = 15) -> list[dict]:
    """The riskiest symbols in the graph, with caller counts for blast context."""
    rows = store.risky_symbols(limit=limit)
    return [
        {
            "name": r[0],
            "path": r[1],
            "line": int(r[2] or 0),
            "fix_count": int(r[3]),
            "last_broke_at": str(r[4]),
            "risk_score": round(float(r[5]), 3),
            "caller_count": int(r[6]),
        }
        for r in rows
    ]


def compute_risk_score(fix_count: int, last_broke_at: str, now: datetime | None = None) -> float:
    """(1 - 0.7^fix_count) decayed by half every 6 months of quiet."""
    if fix_count <= 0:
        return 0.0
    frequency = 1.0 - (0.7**fix_count)
    now = now or datetime.now(timezone.utc)
    try:
        last = datetime.fromisoformat(last_broke_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        months = max(0.0, (now - last).days / 30.44)
    except (ValueError, TypeError):
        months = 12.0
    recency = 0.5 ** (months / 6.0)
    return round(frequency * recency, 4)


def _recompute_risk_scores(store: GraphStore) -> None:
    for path, name, fix_count, last_broke_at in store.symbols_with_fixes():
        store.set_risk_score(path, name, compute_risk_score(int(fix_count), str(last_broke_at)))


# --- git plumbing (read-only, deterministic) ---------------------------------


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _list_fix_commits(repo_root: Path, since: str | None, max_commits: int) -> list[tuple[str, str, str]]:
    # Filtering happens in Python: git's --grep ERE lacks \b on some platforms.
    args = [
        "log",
        f"--max-count={max_commits}",
        "--date=iso-strict",
        "--pretty=format:%H%x1f%ad%x1f%s",
    ]
    if since:
        args.append(f"--since={since}")
    output = _git(repo_root, *args)
    commits = []
    for line in output.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3 and _FIX_RE.search(parts[2]):
            commits.append((parts[0], parts[1], parts[2]))
    return commits


def _commit_diff(repo_root: Path, sha: str) -> str:
    try:
        return _git(repo_root, "show", "--unified=0", "--pretty=format:", sha)
    except subprocess.CalledProcessError:
        return ""


def _attribute_diff(diff: str) -> dict[str, set[str]]:
    """Map file path -> symbol names touched by this diff.

    Symbols come from two deterministic sources: definition lines that appear
    on +/- lines, and the enclosing-function context git puts in hunk headers.
    """
    attribution: dict[str, set[str]] = {}
    current_file: str | None = None
    for line in diff.splitlines():
        file_match = _FILE_HEADER_RE.match(line)
        if file_match:
            path = file_match.group(1)
            current_file = None if path == "/dev/null" else path
            if current_file:
                attribution.setdefault(current_file, set())
            continue
        if current_file is None:
            continue
        hunk_match = _HUNK_HEADER_RE.match(line)
        if hunk_match:
            context = hunk_match.group(1).strip()
            for language in ("python", "typescript", "go", "java"):
                for _, name in _extract_symbols(language, context):
                    attribution[current_file].add(name)
            continue
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            stripped = line[1:].strip()
            for language in ("python", "typescript", "go", "java"):
                for _, name in _extract_symbols(language, stripped):
                    attribution[current_file].add(name)
    return attribution
