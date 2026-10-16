"""Robustness: pathological inputs must not crash parsing or abort indexing."""

from pathlib import Path

from graphora.embedded import EmbeddedGraphStore
from graphora.indexer import index_repository
from graphora.parser import parse_code_file

DEEP_PY = "def f():\n    return " + " + ".join(["x"] * 3000) + "\n\ndef g():\n    return f()\n"


def test_deeply_nested_expression_parses():
    parsed = parse_code_file("deep.py", DEEP_PY)
    assert {s.name for s in parsed.symbols} == {"f", "g"}
    assert any(c.caller == "g" and c.callee == "f" for c in parsed.calls)


def test_one_bad_file_does_not_abort_index(tmp_path: Path, monkeypatch):
    (tmp_path / "good.py").write_text("def ok():\n    return 1\n")
    (tmp_path / "bad.py").write_text("def broken():\n    return 2\n")

    import graphora.indexer as indexer

    real = indexer.parse_code_file

    def flaky(path, content):
        if path == "bad.py":
            raise RuntimeError("parser exploded")
        return real(path, content)

    monkeypatch.setattr(indexer, "parse_code_file", flaky)
    store = EmbeddedGraphStore("robust", data_dir=tmp_path / "data")
    index_repository(tmp_path, project="robust", store=store)
    assert store.find_symbol("ok")
    assert not store.find_symbol("broken")
