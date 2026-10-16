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


def test_signature_survives_form_feed_and_unicode_line_separators():
    src = "x = 1\n\x0c\ns = 'a b'\n\ndef target(a, b):\n    return a\n"
    parsed = parse_code_file("ff.py", src)
    (symbol,) = [s for s in parsed.symbols if s.name == "target"]
    assert symbol.line == 5
    assert symbol.signature == "def target(a, b):"


def test_many_symbols_parse_in_linear_time():
    import time

    src = "".join(f"def f{i}(x):\n    return f{i - 1}(x)\n\n" for i in range(1, 20001))
    start = time.perf_counter()
    parsed = parse_code_file("big.py", src)
    elapsed = time.perf_counter() - start
    assert len(parsed.symbols) == 20000
    assert elapsed < 10, f"parse took {elapsed:.1f}s (quadratic signature lookup?)"
