#!/usr/bin/env python3
"""Public reproducible benchmark: Graphora vs baselines on pinned OSS repos.

Clones each repository at an exact commit, indexes it with the embedded
backend (no server needed), runs the deterministic token-cost benchmark,
and prints one summary table. Anyone can re-run this and get identical
numbers.

Usage:
    python3 scripts/public_benchmark.py [--workdir DIR] [--output FILE]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphora.benchmark import pick_symbols, run_benchmark  # noqa: E402
from graphora.embedded import EmbeddedGraphStore  # noqa: E402
from graphora.indexer import index_repository  # noqa: E402

# Pinned (repo URL, commit SHA) pairs.
REPOS = [
    ("https://github.com/pallets/flask", "36e4a824f340fdee7ed50937ba8e7f6bc7d17f81"),
    ("https://github.com/psf/requests", "f361ead047be5cb873174218582f7d8b9fcd9f49"),
    ("https://github.com/pallets/click", "b67832c2167e5b0ff6764a8c04a0a9087e697b5a"),
    ("https://github.com/FalkorDB/falkordb-py", "971f1d124e57ddf054347e91cdec24b17535196b"),
]


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def clone_at(url: str, sha: str | None, workdir: Path) -> Path:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    dest = workdir / name
    if not dest.exists():
        _run(["git", "clone", "--quiet", url, str(dest)])
    if sha:
        _run(["git", "checkout", "--quiet", sha], cwd=dest)
    return dest


def bench_one(repo_dir: Path, data_dir: Path) -> dict:
    project = f"pubbench-{repo_dir.name}"
    store = EmbeddedGraphStore(project, data_dir=data_dir)
    store.clear()
    index_repository(repo_dir, project=project, store=store)
    symbols = pick_symbols(store, count=3)
    result = run_benchmark(store, repo_dir, symbols=symbols)
    return {
        "repo": repo_dir.name,
        "files": result.file_count,
        "symbols": symbols,
        "repo_tokens": result.repo_tokens,
        "relevant_tokens": result.changed_files_tokens,
        "blast_tokens": result.blast_tokens,
        "saving_vs_repo": round(result.saving_vs_repo, 1),
        "saving_vs_relevant": round(
            (1 - result.blast_tokens / result.changed_files_tokens) * 100 if result.changed_files_tokens else 0.0, 1
        ),
    }


def render(rows: list[dict], shas: dict[str, str]) -> str:
    lines = [
        "# Graphora public benchmark",
        "",
        "Embedded backend (no server), fixed 4 chars/token heuristic, symbols",
        "auto-picked deterministically (most-connected non-test functions).",
        "",
        "| Repository | Commit | Files | Whole repo | Relevant files | Blast radius | Savings vs repo | vs relevant |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in rows:
        sha = shas.get(r["repo"], "HEAD")[:10]
        lines.append(
            f"| {r['repo']} | `{sha}` | {r['files']} | {r['repo_tokens']:,} | {r['relevant_tokens']:,} "
            f"| **{r['blast_tokens']:,}** | **{r['saving_vs_repo']}%** | {r['saving_vs_relevant']}% |"
        )
    lines += ["", "Symbols queried per repo:", ""]
    for r in rows:
        lines.append(f"- {r['repo']}: `{', '.join(r['symbols'])}`")
    lines += ["", "Reproduce: `python3 scripts/public_benchmark.py`", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=None, help="Where to clone repos (default: temp dir)")
    parser.add_argument("--output", default=None, help="Write the markdown table to a file")
    args = parser.parse_args()

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="graphora-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    data_dir = workdir / ".graphora-data"

    rows, shas = [], {}
    for url, sha in REPOS:
        name = url.rsplit("/", 1)[-1]
        print(f"[{name}] cloning + indexing...", file=sys.stderr)
        try:
            repo_dir = clone_at(url, sha, workdir)
        except subprocess.CalledProcessError:
            print(f"[{name}] SKIPPED (clone failed)", file=sys.stderr)
            continue
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True
        ).stdout.strip()
        shas[name] = head
        row = bench_one(repo_dir, data_dir)
        rows.append(row)
        print(
            f"[{name}] {row['saving_vs_repo']}% savings ({row['blast_tokens']:,} vs {row['repo_tokens']:,} tokens)",
            file=sys.stderr,
        )

    report = render(rows, shas)
    print(report)
    if args.output:
        Path(args.output).write_text(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
