"""Graphora CLI: the code graph as a tool, not a GitHub App.

Usage:
  graphora index PATH [--project NAME]
  graphora stats [--project NAME]
  graphora blast SYMBOL [SYMBOL...] [--project NAME] [--json]
  graphora review [--diff FILE | --git-range RANGE --repo PATH] [--project NAME] [--llm]
  graphora risk mine PATH [--project NAME] [--since DATE]
  graphora risk top [--project NAME] [--limit N]
  graphora benchmark PATH [--project NAME] [--symbols A,B]
  graphora serve-mcp [--project NAME]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from graphora import __version__
from graphora.store import open_store


def _store(args: argparse.Namespace):
    return open_store(args.project, backend=args.backend, host=args.host, port=args.port)


def cmd_index(args: argparse.Namespace) -> int:
    from graphora.indexer import index_repository

    def progress(stage: str, current: int, total: int, path: str) -> None:
        sys.stderr.write(f"\r{stage}: {current}/{total} {path[:60]:<60}")
        sys.stderr.flush()

    root = Path(args.path).resolve()
    project = args.project or root.name
    store = open_store(project, backend=args.backend, host=args.host, port=args.port)
    index_repository(root, project=project, store=store, on_progress=progress)
    sys.stderr.write("\n")
    print(json.dumps({"project": project, "graph": store.graph_name, "backend": store.backend, **store.stats()}, indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    print(json.dumps(_store(args).stats(), indent=2))
    return 0


def cmd_blast(args: argparse.Namespace) -> int:
    from graphora.blast import blast_radius

    radius = blast_radius(_store(args), args.symbols)
    print(json.dumps(radius.to_dict(), indent=2) if args.json else radius.to_context())
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from graphora.review import review_diff

    if args.diff:
        diff = Path(args.diff).read_text()
    elif args.git_range:
        repo = Path(args.repo or ".").resolve()
        diff = subprocess.run(
            ["git", "--no-pager", "diff", args.git_range],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
    else:
        diff = sys.stdin.read()
    if not diff.strip():
        print("No diff provided (use --diff FILE, --git-range RANGE, or stdin).", file=sys.stderr)
        return 1
    review = review_diff(_store(args), diff, model=args.model, use_llm=args.llm)
    print(review.render())
    return 0


def cmd_risk_mine(args: argparse.Namespace) -> int:
    from graphora.risk import mine_risk_memory

    root = Path(args.path).resolve()
    project = args.project or root.name
    store = open_store(project, backend=args.backend, host=args.host, port=args.port)
    stats = mine_risk_memory(store, root, since=args.since, max_commits=args.max_commits)
    print(json.dumps(stats.__dict__, indent=2))
    return 0


def cmd_risk_top(args: argparse.Namespace) -> int:
    from graphora.risk import risk_report

    report = risk_report(_store(args), limit=args.limit)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    if not report:
        print("No risk memory yet. Run: graphora risk mine <repo-path>")
        return 0
    print(f"{'RISK':>6}  {'FIXES':>5}  {'CALLERS':>7}  {'LAST BROKE':<12} SYMBOL")
    for row in report:
        print(
            f"{row['risk_score']:>6.2f}  {row['fix_count']:>5}  {row['caller_count']:>7}  "
            f"{row['last_broke_at'][:10]:<12} {row['name']}  ({row['path']}:{row['line']})"
        )
    return 0


def cmd_benchmark(args: argparse.Namespace) -> int:
    from graphora.benchmark import run_benchmark

    root = Path(args.path).resolve()
    project = args.project or root.name
    store = open_store(project, backend=args.backend, host=args.host, port=args.port)
    symbols = [s for s in (args.symbols or "").split(",") if s] or None
    result = run_benchmark(store, root, symbols=symbols)
    print(result.render())
    if args.output:
        Path(args.output).write_text(result.render() + "\n")
    return 0


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    from graphora.mcp_server import serve

    serve(project=args.project, host=args.host, port=args.port, backend=args.backend)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="graphora", description="Deterministic code knowledge graph tool")
    parser.add_argument("--version", action="version", version=f"graphora {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=None, help="Graph project name (default: directory name)")
    common.add_argument("--host", default="localhost", help="FalkorDB host")
    common.add_argument("--port", type=int, default=6379, help="FalkorDB port")
    common.add_argument(
        "--backend",
        choices=["auto", "falkordb", "embedded"],
        default="auto",
        help="Graph storage: FalkorDB server, embedded JSON (no Docker), or auto-detect (default)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", parents=[common], help="Index a repository into the graph (no LLM)")
    p_index.add_argument("path")
    p_index.set_defaults(func=cmd_index)

    p_stats = sub.add_parser("stats", parents=[common], help="Show graph stats")
    p_stats.set_defaults(func=cmd_stats, project_required=True)

    p_blast = sub.add_parser("blast", parents=[common], help="Blast radius for symbols")
    p_blast.add_argument("symbols", nargs="+")
    p_blast.add_argument("--json", action="store_true")
    p_blast.set_defaults(func=cmd_blast)

    p_review = sub.add_parser("review", parents=[common], help="Review a diff grounded in the graph")
    p_review.add_argument("--diff", help="Path to a unified diff file")
    p_review.add_argument("--git-range", help="Git range, e.g. main...HEAD")
    p_review.add_argument("--repo", help="Repo path for --git-range (default: cwd)")
    p_review.add_argument("--llm", action="store_true", help="Use an LLM (REVIEW_MODEL env or --model)")
    p_review.add_argument("--model", default=None)
    p_review.set_defaults(func=cmd_review)

    p_risk = sub.add_parser("risk", parents=[common], help="Risk memory commands")
    risk_sub = p_risk.add_subparsers(dest="risk_command", required=True)
    p_mine = risk_sub.add_parser("mine", parents=[common], help="Mine git fix/revert history into the graph")
    p_mine.add_argument("path")
    p_mine.add_argument("--since", default=None, help="e.g. '2 years ago'")
    p_mine.add_argument("--max-commits", type=int, default=2000)
    p_mine.set_defaults(func=cmd_risk_mine)
    p_top = risk_sub.add_parser("top", parents=[common], help="Riskiest symbols")
    p_top.add_argument("--limit", type=int, default=15)
    p_top.add_argument("--json", action="store_true")
    p_top.set_defaults(func=cmd_risk_top)

    p_bench = sub.add_parser("benchmark", parents=[common], help="Reproducible token-cost benchmark")
    p_bench.add_argument("path")
    p_bench.add_argument("--symbols", help="Comma-separated symbols to query (default: auto-pick)")
    p_bench.add_argument("--output", help="Write the report to a file")
    p_bench.set_defaults(func=cmd_benchmark)

    p_mcp = sub.add_parser("serve-mcp", parents=[common], help="Serve the graph as an MCP stdio server")
    p_mcp.set_defaults(func=cmd_serve_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "project", None) is None and args.command not in {"index", "benchmark"}:
        if args.command == "review" and args.repo:
            args.project = Path(args.repo).resolve().name
        elif args.command == "risk" and getattr(args, "path", None):
            args.project = Path(args.path).resolve().name
        else:
            args.project = Path.cwd().name
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
