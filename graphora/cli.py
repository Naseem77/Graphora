"""Graphora CLI: the code graph as a tool, not a GitHub App.

Usage:
  graphora index PATH [--project NAME]
  graphora stats [--project NAME]
  graphora breaks SYMBOL [SYMBOL...] [--depth N] [--json]
  graphora breaks --diff FILE | --git-range RANGE [--repo PATH]
  graphora blast SYMBOL [SYMBOL...] [--project NAME] [--json]
  graphora review [--diff FILE | --git-range RANGE --repo PATH] [--project NAME] [--llm]
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
    """Open the project graph for a read command, warning if nothing is indexed."""
    store = open_store(args.project, backend=args.backend, host=args.host, port=args.port)
    if not store.has_files():
        print(
            f"graphora: warning: graph {store.graph_name!r} ({store.backend}) is empty. "
            f"Run 'graphora index <repo-path>' first, or pass --project "
            f"(it defaults to the current directory name).",
            file=sys.stderr,
        )
    return store


def cmd_index(args: argparse.Namespace) -> int:
    from graphora.indexer import index_repository

    def progress(stage: str, current: int, total: int, path: str) -> None:
        sys.stderr.write(f"\r{stage}: {current}/{total} {path[:60]:<60}")
        sys.stderr.flush()

    root = Path(args.path).resolve()
    project = args.project or root.name
    store = open_store(project, backend=args.backend, host=args.host, port=args.port)
    try:
        index_repository(
            root, project=project, store=store, on_progress=progress, include_generated=args.include_generated
        )
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        print(f"graphora: error: {exc}", file=sys.stderr)
        return 1
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


def _read_diff(args: argparse.Namespace, allow_stdin: bool = True) -> str:
    if args.diff:
        return Path(args.diff).read_text()
    if args.git_range:
        repo = Path(args.repo or ".").resolve()
        return subprocess.run(
            ["git", "--no-pager", "diff", args.git_range],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
    return sys.stdin.read() if allow_stdin else ""


def cmd_breaks(args: argparse.Namespace) -> int:
    from graphora.impact import what_could_break, what_could_break_for_diff

    store = _store(args)
    if args.symbols:
        result = what_could_break(store, args.symbols, depth=args.depth)
    else:
        diff = _read_diff(args)
        if not diff.strip():
            print("Give symbols, or a diff (--diff FILE, --git-range RANGE, or stdin).", file=sys.stderr)
            return 1
        result = what_could_break_for_diff(store, diff, depth=args.depth)
    print(json.dumps(result.to_dict(), indent=2) if args.json else result.to_text())
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from graphora.review import review_diff

    diff = _read_diff(args)
    if not diff.strip():
        print("No diff provided (use --diff FILE, --git-range RANGE, or stdin).", file=sys.stderr)
        return 1
    review = review_diff(_store(args), diff, model=args.model, use_llm=args.llm)
    print(review.render())
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


def cmd_install_skill(args: argparse.Namespace) -> int:
    from graphora.skills import install_skill, list_agents

    if args.list:
        print("\n".join(list_agents()))
        return 0
    agents = None if not args.agents or args.agents == ["all"] else args.agents
    kinds = ["code", "sessions"] if args.skill == "all" else [args.skill]
    try:
        written = []
        for kind in kinds:
            written.extend(install_skill(args.repo, agents, skill=kind))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"repo": str(Path(args.repo).resolve()), "written": written}, indent=2))
    return 0


def cmd_sessions_ingest(args: argparse.Namespace) -> int:
    from graphora.sessions import ingest_sources

    store = open_store(args.project or "agent-sessions", backend=args.backend, host=args.host, port=args.port)
    sources = None if args.source == "all" else [args.source]
    paths = {}
    if args.db:
        paths["copilot"] = args.db
    if args.claude_root:
        paths["claude"] = args.claude_root
    if args.codex_root:
        paths["codex"] = args.codex_root
    results = ingest_sources(store, sources=sources, days=args.days, paths=paths)
    print(json.dumps({"project": store.project, "graph": store.graph_name, "sources": results}, indent=2))
    return 0


def cmd_sessions_connected(args: argparse.Namespace) -> int:
    from graphora.sessions import connected

    store = open_store(args.project or "agent-sessions", backend=args.backend, host=args.host, port=args.port)
    rows = connected(store, args.kind, args.value)
    print(json.dumps(rows, indent=2))
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

    # Nested subcommands re-declare the common flags so they work after the
    # sub-subcommand too; SUPPRESS keeps their defaults from overwriting values
    # already given before it (argparse applies the inner parser's defaults last).
    nested_common = argparse.ArgumentParser(add_help=False)
    for action in common._actions:
        options: dict = {"dest": action.dest, "help": action.help, "default": argparse.SUPPRESS}
        if action.type is not None:
            options["type"] = action.type
        if action.choices is not None:
            options["choices"] = action.choices
        nested_common.add_argument(*action.option_strings, **options)

    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", parents=[common], help="Index a repository into the graph (no LLM)")
    p_index.add_argument("path")
    p_index.add_argument(
        "--include-generated",
        action="store_true",
        help="Also index generated files (ANTLR/protobuf output, '@generated', minified JS)",
    )
    p_index.set_defaults(func=cmd_index)

    p_stats = sub.add_parser("stats", parents=[common], help="Show graph stats")
    p_stats.set_defaults(func=cmd_stats, project_required=True)

    p_breaks = sub.add_parser(
        "breaks", parents=[common], help="What could break if you change these symbols (or this diff)"
    )
    p_breaks.add_argument("symbols", nargs="*", help="Names or qualified names (Class.method); omit to read a diff")
    p_breaks.add_argument("--depth", type=int, default=2, help="Caller hops to follow (default: 2)")
    p_breaks.add_argument("--diff", help="Path to a unified diff file")
    p_breaks.add_argument("--git-range", help="Git range, e.g. main...HEAD")
    p_breaks.add_argument("--repo", help="Repo path for --git-range (default: cwd)")
    p_breaks.add_argument("--json", action="store_true")
    p_breaks.set_defaults(func=cmd_breaks)

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

    p_bench = sub.add_parser("benchmark", parents=[common], help="Reproducible token-cost benchmark")
    p_bench.add_argument("path")
    p_bench.add_argument("--symbols", help="Comma-separated symbols to query (default: auto-pick)")
    p_bench.add_argument("--output", help="Write the report to a file")
    p_bench.set_defaults(func=cmd_benchmark)

    p_mcp = sub.add_parser("serve-mcp", parents=[common], help="Serve the graph as an MCP stdio server")
    p_mcp.set_defaults(func=cmd_serve_mcp)

    p_sess = sub.add_parser("sessions", parents=[common], help="Agent session-history graph commands")
    sess_sub = p_sess.add_subparsers(dest="sessions_command", required=True)
    p_si = sess_sub.add_parser("ingest", parents=[nested_common], help="Ingest agent session stores into the graph")
    p_si.add_argument("--source", choices=["all", "copilot", "claude", "codex"], default="all",
                      help="Which agent's sessions to ingest (default: all found)")
    p_si.add_argument("--db", default=None, help="Copilot session-store.db path (default: ~/.copilot/session-store.db)")
    p_si.add_argument("--claude-root", default=None, help="Claude Code projects dir (default: ~/.claude/projects)")
    p_si.add_argument("--codex-root", default=None, help="Codex CLI sessions dir (default: ~/.codex/sessions)")
    p_si.add_argument("--days", type=int, default=30, help="Only sessions updated in the last N days")
    p_si.set_defaults(func=cmd_sessions_ingest)
    p_sc = sess_sub.add_parser("connected", parents=[nested_common], help="Sessions connected to a file, repo, or PR/issue/commit")
    p_sc.add_argument("kind", choices=["file", "repo", "ref"])
    p_sc.add_argument("value")
    p_sc.set_defaults(func=cmd_sessions_connected)

    p_skill = sub.add_parser("install-skill", help="Install the Graphora skill/rule for AI coding agents")
    p_skill.add_argument("agents", nargs="*", help="Agent names, or 'all' (default: all)")
    p_skill.add_argument("--repo", default=".", help="Repository root to install into (default: cwd)")
    p_skill.add_argument("--list", action="store_true", help="List supported agents")
    p_skill.add_argument("--skill", choices=["code", "sessions", "all"], default="code",
                         help="Which skill to install: code graph workflow, session recall, or both")
    p_skill.set_defaults(func=cmd_install_skill)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "project", None) is None and args.command not in {"index", "benchmark"}:
        if args.command in {"review", "breaks"} and getattr(args, "repo", None):
            args.project = Path(args.repo).resolve().name
        elif args.command == "sessions":
            args.project = "agent-sessions"
        else:
            args.project = Path.cwd().name
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
