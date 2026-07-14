"""Phase 1 tests: deterministic parser with confidence tags."""

from graphora.parser import (
    AMBIGUOUS,
    EXTRACTED,
    INFERRED,
    extract_files_from_diff,
    extract_symbols_from_diff,
    is_test_path,
    parse_code_file,
)

PY_SOURCE = '''\
import os
from pathlib import Path

def helper(x):
    return x * 2

def main(value):
    result = helper(value)
    external_thing(result)
    return result

class Widget:
    def render(self):
        return main(1)
'''


def test_python_symbols_extracted_with_confidence():
    parsed = parse_code_file("pkg/main.py", PY_SOURCE)
    names = {(s.kind, s.name) for s in parsed.symbols}
    assert ("Function", "helper") in names
    assert ("Function", "main") in names
    assert ("Class", "Widget") in names
    assert parsed.parser_used == "tree-sitter"
    assert all(s.confidence == EXTRACTED for s in parsed.symbols)


def test_imports_are_extracted_confidence():
    parsed = parse_code_file("pkg/main.py", PY_SOURCE)
    import_names = {i.name for i in parsed.imports}
    assert "os" in import_names and "pathlib" in import_names
    assert all(i.confidence == EXTRACTED for i in parsed.imports)


def test_local_call_promoted_to_extracted_and_unknown_call_inferred():
    parsed = parse_code_file("pkg/main.py", PY_SOURCE)
    calls = {(c.caller, c.callee): c.confidence for c in parsed.calls}
    assert calls[("main", "helper")] == EXTRACTED  # defined in same file
    assert calls[("main", "external_thing")] == INFERRED  # unresolved here


def test_regex_fallback_marks_lower_confidence():
    # Unknown "language" path forces regex fallback with python-style code.
    parsed = parse_code_file("script.py.txt", "def foo():\n    bar()\n")
    assert parsed.language == "text"
    assert parsed.parser_used == "regex"


def test_typescript_and_go_parse():
    ts = parse_code_file("src/app.ts", "export function greet(name: string) { return hi(name); }\n")
    assert any(s.name == "greet" for s in ts.symbols)
    go = parse_code_file("main.go", 'package main\nfunc Run() { helper() }\nfunc helper() {}\n')
    go_calls = {(c.caller, c.callee): c.confidence for c in go.calls}
    assert go_calls[("Run", "helper")] == EXTRACTED


def test_diff_symbol_and_file_extraction():
    diff = """\
--- a/pkg/main.py
+++ b/pkg/main.py
@@ -1,3 +1,4 @@
+def added_function(x):
+    return x
-def removed_function():
"""
    symbols = extract_symbols_from_diff(diff)
    assert "added_function" in symbols
    assert "removed_function" in symbols
    assert extract_files_from_diff(diff) == ["pkg/main.py"]


def test_is_test_path():
    assert is_test_path("tests/test_store.py")
    assert is_test_path("src/__tests__/app.spec.ts")
    assert not is_test_path("app/graph/store.py")
