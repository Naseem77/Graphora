"""Unified diff handling: headers, hunks, deletions, deleted functions."""

import time
from pathlib import Path

import pytest

from graphora.embedded import EmbeddedGraphStore
from graphora.impact import changed_symbols, what_could_break_for_diff
from graphora.indexer import index_repository
from graphora.parser import extract_files_from_diff, extract_symbols_from_diff, parse_unified_diff


def test_gnu_and_quoted_headers():
    gnu = "--- old/m.py\t2024-01-01 10:00:00\n+++ new/m.py\t2024-01-02 10:00:00\n@@ -1 +1 @@\n-a\n+b\n"
    assert extract_files_from_diff(gnu) == ["new/m.py"]
    quoted = '--- "a/sp\\303\\244ce.py"\n+++ "b/sp\\303\\244ce.py"\n@@ -1 +1 @@\n-a\n+b\n'
    assert extract_files_from_diff(quoted) == ["späce.py"]


def test_lines_that_look_like_headers_inside_a_hunk_are_content():
    diff = (
        "--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,2 @@\n"
        "--- removed line starting with dashes\n"
        "+++ added line starting with pluses\n"
        " def keep():\n"
        "--- a/n.py\n+++ b/n.py\n@@ -1 +1 @@\n-def gone():\n+def here():\n"
    )
    files = parse_unified_diff(diff)
    assert [f.path for f in files] == ["m.py", "n.py"]
    assert files[0].added == [(1, "++ added line starting with pluses")]
    assert files[0].removed[0][2] == "-- removed line starting with dashes"
    assert extract_symbols_from_diff(diff) == ["gone", "here"]


@pytest.fixture()
def store(tmp_path: Path) -> EmbeddedGraphStore:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lib.py").write_text(
        "def f(x):\n"  # 1
        "    y = x\n"  # 2
        "    return y\n"  # 3
        "def g():\n"  # 4
        "    return 2\n"  # 5
    )
    (repo / "app.py").write_text(
        "from lib import f, g\n\ndef uses_f():\n    return f(1)\n\ndef uses_g():\n    return g()\n"
    )
    store = EmbeddedGraphStore("diff", data_dir=tmp_path / "data")
    index_repository(repo, project="diff", store=store)
    return store


def test_deleting_the_last_lines_of_a_function_targets_that_function(store):
    # Old f had a 4th line (`    log(y)`) just before g; the graph holds the new code.
    diff = "--- a/lib.py\n+++ b/lib.py\n@@ -3,3 +3,2 @@\n     return y\n-    log(y)\n def g():\n"
    targets, _ = changed_symbols(store, diff)
    assert [t["qualname"] for t in targets] == ["f"]


def test_deleted_function_reports_its_now_broken_callers(tmp_path):
    repo = tmp_path / "repo2"
    repo.mkdir()
    (repo / "lib.py").write_text("def other():\n    return 1\n")  # `gone` already deleted (new code)
    (repo / "app.py").write_text("from lib import gone\n\ndef caller():\n    return gone()\n")
    store = EmbeddedGraphStore("deleted", data_dir=tmp_path / "data")
    index_repository(repo, project="deleted", store=store)
    diff = "--- a/lib.py\n+++ b/lib.py\n@@ -1,4 +1,2 @@\n-def gone():\n-    return 0\n def other():\n     return 1\n"
    result = what_could_break_for_diff(store, diff)
    assert result.deleted == ["gone"]
    assert [b.caller["qualname"] for b in result.will_break] == ["caller"]
    text = result.to_text()
    assert "Deleted: `gone`" in text and "`caller`" in text


def test_star_import_cycles_resolve_quickly():
    from graphora.parser import parse_code_file
    from graphora.resolve import records_for, resolve

    files = {f"m{i}.py": "".join(f"from m{j} import *\n" for j in range(30) if j != i) for i in range(30)}
    files["use.py"] = "from m0 import *\n\ndef run():\n" + "".join(f"    missing{k}()\n" for k in range(5))
    symbols, records = [], []
    for path, source in files.items():
        s, r = records_for(parse_code_file(path, source))
        symbols.extend(s)
        records.append(r)
    start = time.perf_counter()
    resolve(symbols, records)
    assert time.perf_counter() - start < 2.0


def test_deleting_a_method_does_not_target_its_whole_class(tmp_path):
    repo = tmp_path / "cls"
    repo.mkdir()
    (repo / "g.py").write_text(
        "class Graph:\n    def keep(self):\n        return 1\n\n    def other(self):\n        return 2\n"
    )
    (repo / "t_use.py").write_text("from g import Graph\n\ndef use(g: Graph):\n    return g.keep()\n")
    store = EmbeddedGraphStore("cls", data_dir=tmp_path / "data")
    index_repository(repo, project="cls", store=store)
    diff = (
        "--- a/g.py\n+++ b/g.py\n@@ -3,6 +3,3 @@\n         return 1\n \n"
        "-    def dropped(self):\n-        return 0\n-\n     def other(self):\n"
    )
    result = what_could_break_for_diff(store, diff)
    assert [t["qualname"] for t in result.targets] == []
    assert result.deleted == ["dropped"]


def test_deleted_method_with_a_same_named_survivor_lists_uncertain_callers(tmp_path):
    repo = tmp_path / "twins"
    (repo / "tests").mkdir(parents=True)
    (repo / "sync.py").write_text("class Graph:\n    def keep(self):\n        return 1\n")  # drop() deleted
    (repo / "aio.py").write_text("class AsyncGraph:\n    def drop(self):\n        return 2\n")
    (repo / "tests" / "test_sync.py").write_text("def test_drop(graph):\n    graph.drop()\n")
    store = EmbeddedGraphStore("twins2", data_dir=tmp_path / "data")
    index_repository(repo, project="twins2", store=store)
    diff = "--- a/sync.py\n+++ b/sync.py\n@@ -3,3 +3,0 @@\n-    def drop(self):\n-        return 0\n-\n"
    result = what_could_break_for_diff(store, diff)
    assert result.deleted == ["drop"]
    assert [(b.caller["qualname"], b.certain) for b in result.tests] == [("test_drop", False)]
