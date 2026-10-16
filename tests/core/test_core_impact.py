"""What could break: certain vs uncertain impact, with the alternatives spelled out."""

from pathlib import Path

import pytest

from graphora.embedded import EmbeddedGraphStore
from graphora.impact import changed_symbols, what_could_break, what_could_break_for_diff
from graphora.indexer import index_repository
from graphora.parser import extract_symbols_from_diff


@pytest.fixture()
def store(tmp_path: Path) -> EmbeddedGraphStore:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "db.py").write_text(
        "class Db:\n"
        "    def save(self, row):\n"
        "        return row\n"
    )
    (repo / "app" / "files.py").write_text(
        "class FileStore:\n"
        "    def save(self, row):\n"
        "        return row\n"
    )
    (repo / "app" / "service.py").write_text(
        "from app.db import Db\n"
        "\n"
        "def record(row):\n"
        "    return Db().save(row)\n"
        "\n"
        "def persist(store, row):\n"
        "    return store.save(row)\n"
        "\n"
        "def checkout(cart):\n"
        "    return record(cart)\n"
    )
    (repo / "app" / "api.py").write_text(
        "from app.service import checkout\n"
        "\n"
        "def post_checkout(req):\n"
        "    return checkout(req)\n"
    )
    (repo / "tests" / "test_service.py").write_text(
        "from app.service import record\n"
        "\n"
        "def test_record():\n"
        "    assert record(1) == 1\n"
    )
    store = EmbeddedGraphStore("impact", data_dir=tmp_path / "data")
    index_repository(repo, project="impact", store=store)
    return store


def _names(entries):
    return {b.caller["qualname"] for b in entries}


def test_direct_and_transitive_callers_will_break(store):
    result = what_could_break(store, ["record"], depth=3)
    assert _names(result.will_break) == {"checkout", "post_checkout"}
    by_name = {b.caller["qualname"]: b for b in result.will_break}
    assert by_name["checkout"].depth == 1
    assert by_name["post_checkout"].depth == 2
    assert by_name["post_checkout"].via == ("checkout", "record")
    assert _names(result.tests) == {"test_record"}


def test_depth_limits_the_walk(store):
    assert _names(what_could_break(store, ["record"], depth=1).will_break) == {"checkout"}


def test_ambiguous_call_is_listed_with_its_alternatives(store):
    result = what_could_break(store, ["Db.save"], depth=1)
    (persist,) = [b for b in result.might_break if b.caller["qualname"] == "persist"]
    options = {(a.qualname, a.is_target) for a in persist.alternatives}
    assert options == {("Db.save", True), ("FileStore.save", False)}
    assert persist.may_be_external
    text = result.to_text()
    assert "Might break (1)" in text
    assert "this `save(...)` call is either" in text
    assert "`Db.save` (app/db.py:2)  <- the one you are changing" in text
    assert "or `FileStore.save` (app/files.py:2)" in text
    assert "or a method outside this repository" in text


def test_import_backed_call_is_certain(store):
    result = what_could_break(store, ["Db.save"], depth=1)
    assert "record" in _names(result.will_break)


def test_class_includes_callers_of_its_methods(store):
    result = what_could_break(store, ["Db"], depth=1)
    assert "record" in _names(result.will_break)


def test_uncertainty_propagates_up_the_chain(store):
    result = what_could_break(store, ["FileStore.save"], depth=2)
    assert _names(result.will_break) == set()
    assert "persist" in _names(result.might_break)


def test_same_name_targets_are_all_reported(store):
    result = what_could_break(store, ["save"], depth=1)
    assert {t["qualname"] for t in result.targets} == {"Db.save", "FileStore.save"}
    assert "several definitions are named `save`" in result.to_text()


def test_unknown_symbol(store):
    result = what_could_break(store, ["nope"])
    assert result.unresolved == ["nope"]
    assert "No definition found for `nope`" in result.to_text()


def test_diff_body_edit_finds_enclosing_function(store):
    diff = (
        "--- a/app/service.py\n+++ b/app/service.py\n"
        "@@ -3,2 +3,2 @@\n def record(row):\n-    return Db().save(row)\n+    return Db().save(row) or None\n"
    )
    targets, _ = changed_symbols(store, diff)
    assert [t["qualname"] for t in targets] == ["record"]
    result = what_could_break_for_diff(store, diff)
    assert "checkout" in _names(result.will_break)


def test_diff_method_body_edit(store):
    diff = "--- a/app/db.py\n+++ b/app/db.py\n@@ -3 +3 @@\n-        return row\n+        return dict(row)\n"
    targets, _ = changed_symbols(store, diff)
    assert [t["qualname"] for t in targets] == ["Db.save"]


def test_to_dict_is_json_ready(store):
    import json

    data = what_could_break(store, ["Db.save"]).to_dict()
    json.dumps(data)
    assert data["targets"][0]["name"] == "Db.save"
    assert data["might_break"][0]["alternatives"]


def test_diff_symbols_use_the_file_language():
    diff = "--- a/pkg/x.py\n+++ b/pkg/x.py\n@@ -1 +1 @@\n+    return len(items)\n+def real(x):\n"
    assert extract_symbols_from_diff(diff) == ["real"]


def test_cli_breaks(store, tmp_path, capsys, monkeypatch):
    from graphora.cli import main

    monkeypatch.setenv("GRAPHORA_DATA_DIR", str(tmp_path / "data"))
    assert main(["breaks", "record", "--project", "impact", "--backend", "embedded"]) == 0
    out = capsys.readouterr().out
    assert "Will break (2)" in out and "`post_checkout`" in out
    diff = tmp_path / "c.diff"
    diff.write_text("--- a/app/db.py\n+++ b/app/db.py\n@@ -3 +3 @@\n-        return row\n+        return dict(row)\n")
    assert main(["breaks", "--diff", str(diff), "--project", "impact", "--backend", "embedded", "--json"]) == 0
    import json

    data = json.loads(capsys.readouterr().out)
    assert data["targets"][0]["name"] == "Db.save"


def test_falkordb_gives_the_same_answers(store, tmp_path):
    """Both backends must agree on will/might break and the X-or-Y alternatives."""
    from graphora.store import GraphStore, falkordb_available

    if not falkordb_available():
        pytest.skip("FalkorDB not running on localhost:6379")
    falkor = GraphStore("graphora-impact-parity")
    try:
        index_repository(tmp_path / "repo", project="graphora-impact-parity", store=falkor)
        for names in (["Db.save"], ["record"], ["save"], ["FileStore"]):
            assert what_could_break(falkor, names, depth=3).to_dict() == what_could_break(store, names, depth=3).to_dict()
    finally:
        falkor.delete_graph()
