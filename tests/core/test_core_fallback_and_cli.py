"""The regex fallback parser, the sessions/benchmark CLI, and backend parity for deletions."""

import json
import re
import sqlite3
from pathlib import Path

import pytest

import graphora.parser as parser_module
from graphora.cli import main
from graphora.embedded import EmbeddedGraphStore
from graphora.impact import what_could_break_for_diff
from graphora.indexer import index_repository
from graphora.parser import INFERRED, parse_code_file

FALLBACK_SOURCES = {
    "a.py": ("class Widget:\n    pass\n\ndef render(x):\n    return helper(x)\n", {"Widget", "render"}, ("render", "helper")),
    "a.ts": ("export class Cart {}\nexport function total(x) { return sum(x); }\n", {"Cart", "total"}, ("total", "sum")),
    "a.go": ("package p\ntype Rect struct{}\nfunc Area() int { return calc() }\n", {"Rect", "Area"}, ("Area", "calc")),
    "A.java": ("public class Svc {\npublic int run() { return go(); }\n}\n", {"Svc", "run"}, ("run", "go")),
    "a.rs": ("pub struct P;\npub fn area() -> f64 { calc() }\n", {"P", "area"}, ("area", "calc")),
    "a.c": ("struct point { int x; };\nint add(int a)\n{ return helper(a); }\n", {"point", "add"}, ("add", "helper")),
    "a.rb": ("class Cart\nend\ndef total\n  sum(1)\nend\n", {"Cart", "total"}, ("total", "sum")),
    "a.php": ("<?php\nclass Cart {}\nfunction total() { return sum(1); }\n", {"Cart", "total"}, ("total", "sum")),
}


@pytest.mark.parametrize("path", sorted(FALLBACK_SOURCES))
def test_regex_fallback_when_no_grammar_is_installed(path, monkeypatch):
    monkeypatch.setattr(parser_module, "_tree_sitter_parser", lambda language: None)
    source, names, call = FALLBACK_SOURCES[path]
    parsed = parse_code_file(path, source)
    assert parsed.parser_used == "regex"
    assert names <= {s.name for s in parsed.symbols}
    assert all(s.confidence == INFERRED for s in parsed.symbols)
    assert call in {(c.caller, c.callee) for c in parsed.calls}


def test_index_and_breaks_work_on_the_fallback_parser(tmp_path, monkeypatch):
    monkeypatch.setattr(parser_module, "_tree_sitter_parser", lambda language: None)
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "lib.py").write_text("def helper(x):\n    return x\n")
    (repo / "app.py").write_text("def run():\n    return helper(1)\n")
    store = EmbeddedGraphStore("fallback", data_dir=tmp_path / "data")
    index_repository(repo, project="fallback", store=store)
    from graphora.impact import what_could_break

    assert [b.caller["name"] for b in what_could_break(store, ["helper"]).will_break] == ["run"]


def _session_db(path: Path) -> Path:
    source = Path(__file__).with_name("test_core_sessions.py").read_text()
    sql = re.search(r'con\.executescript\(\s*"""(.*?)"""', source, re.S).group(1)
    con = sqlite3.connect(path)
    con.executescript(sql)
    con.commit()
    con.close()
    return path


def test_cli_sessions_ingest_and_connected(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GRAPHORA_DATA_DIR", str(tmp_path / "data"))
    db = _session_db(tmp_path / "store.db")
    assert main(["sessions", "ingest", "--source", "copilot", "--db", str(db), "--days", "30", "--backend", "embedded"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sources"]["copilot"] == {"sessions": 3, "files_touched": 3, "refs": 2}
    assert main(["sessions", "connected", "file", "build.yml", "--backend", "embedded"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["session"] for r in rows} == {"aaa11111", "bbb22222"}
    assert {r["agent"] for r in rows} == {"copilot"}


def test_cli_benchmark_writes_a_report(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GRAPHORA_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "bench"
    repo.mkdir()
    (repo / "core.py").write_text("def hub(x):\n    return x\n\ndef a():\n    return hub(1)\n")
    assert main(["index", str(repo), "--backend", "embedded"]) == 0
    report = tmp_path / "report.md"
    assert main(["benchmark", str(repo), "--backend", "embedded", "--output", str(report)]) == 0
    assert "## Accuracy" in report.read_text() and "2/2" in report.read_text()


def test_deleted_function_callers_match_on_falkordb(tmp_path):
    from graphora.store import GraphStore, falkordb_available

    if not falkordb_available():
        pytest.skip("FalkorDB not running on localhost:6379")
    repo = tmp_path / "del"
    (repo / "tests").mkdir(parents=True)
    (repo / "lib.py").write_text("def other():\n    return 1\n")
    (repo / "app.py").write_text("from lib import gone\n\ndef caller():\n    return gone()\n")
    (repo / "aio.py").write_text("class AsyncGraph:\n    def drop(self):\n        return 2\n")
    (repo / "tests" / "test_x.py").write_text("def test_drop(graph):\n    graph.drop()\n")
    diff = (
        "--- a/lib.py\n+++ b/lib.py\n@@ -1,6 +1,2 @@\n-def gone():\n-    return 0\n-def drop():\n-    return 0\n"
        " def other():\n     return 1\n"
    )
    embedded = EmbeddedGraphStore("del", data_dir=tmp_path / "data")
    index_repository(repo, project="del", store=embedded)
    falkor = GraphStore("graphora-deleted-parity")
    try:
        index_repository(repo, project="graphora-deleted-parity", store=falkor)
        a = what_could_break_for_diff(embedded, diff).to_dict()
        b = what_could_break_for_diff(falkor, diff).to_dict()
        assert a == b
        assert a["deleted"] == ["gone", "drop"]
        assert [x["name"] for x in a["will_break"]] == ["caller"]
        assert [(x["name"], x["certain"]) for x in a["tests"]] == [("test_drop", False)]
    finally:
        falkor.delete_graph()
