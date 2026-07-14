"""New-language parsing tests: Rust, C, C++, Ruby, PHP (tree-sitter path)."""

import pytest

from graphora.parser import is_supported_code_file, language_for_path, parse_code_file

RUST = """\
use std::fmt;

fn helper(x: i32) -> i32 { x }

fn add(a: i32) -> i32 {
    helper(a)
}

struct Point { x: i32 }

trait Shape {}
"""

C = """\
#include <stdio.h>
#include "local.h"

int helper(int x) { return x; }

int add(int a) {
    return helper(a);
}

struct point { int x; };
"""

CPP = """\
#include <vector>

class Point {
public:
    int area();
};

int helper(int x) { return x; }

int Point::area() {
    return helper(1);
}
"""

RUBY = """\
require 'json'

def helper(x)
  x
end

class Cart
  def total
    helper(1)
  end
end
"""

PHP = """\
<?php
use App\\Models\\User;

function helper($x) { return $x; }

function add($a) {
    return helper($a);
}

class Cart {
    public function total() { return 1; }
}
"""


@pytest.mark.parametrize(
    "path,language",
    [
        ("src/lib.rs", "rust"),
        ("src/main.c", "c"),
        ("src/util.h", "c"),
        ("src/app.cpp", "cpp"),
        ("src/app.hpp", "cpp"),
        ("lib/cart.rb", "ruby"),
        ("app/Cart.php", "php"),
    ],
)
def test_language_detection(path: str, language: str):
    assert is_supported_code_file(path)
    assert language_for_path(path) == language


def test_rust_symbols_imports_calls():
    parsed = parse_code_file("src/lib.rs", RUST)
    assert parsed.parser_used == "tree-sitter"
    names = {(s.kind, s.name) for s in parsed.symbols}
    assert ("Function", "add") in names
    assert ("Class", "Point") in names
    assert ("Class", "Shape") in names
    assert any(i.name.startswith("std::fmt") for i in parsed.imports)
    assert any(c.caller == "add" and c.callee == "helper" for c in parsed.calls)
    same_file = [c for c in parsed.calls if c.callee == "helper"]
    assert all(c.confidence == "EXTRACTED" for c in same_file)


def test_c_symbols_imports_calls():
    parsed = parse_code_file("src/main.c", C)
    assert parsed.parser_used == "tree-sitter"
    names = {(s.kind, s.name) for s in parsed.symbols}
    assert ("Function", "add") in names
    assert ("Class", "point") in names
    imports = {i.name for i in parsed.imports}
    assert "stdio.h" in imports and "local.h" in imports
    assert any(c.caller == "add" and c.callee == "helper" for c in parsed.calls)


def test_cpp_class_and_method():
    parsed = parse_code_file("src/app.cpp", CPP)
    assert parsed.parser_used == "tree-sitter"
    names = {(s.kind, s.name) for s in parsed.symbols}
    assert ("Class", "Point") in names
    assert ("Function", "area") in names  # qualified Point::area resolved to area
    assert any(c.caller == "area" and c.callee == "helper" for c in parsed.calls)


def test_ruby_symbols_and_require():
    parsed = parse_code_file("lib/cart.rb", RUBY)
    assert parsed.parser_used == "tree-sitter"
    names = {(s.kind, s.name) for s in parsed.symbols}
    assert ("Class", "Cart") in names
    assert ("Function", "total") in names
    assert any(i.name == "json" for i in parsed.imports)
    assert any(c.caller == "total" and c.callee == "helper" for c in parsed.calls)


def test_php_symbols_use_and_calls():
    parsed = parse_code_file("app/Cart.php", PHP)
    assert parsed.parser_used == "tree-sitter"
    names = {(s.kind, s.name) for s in parsed.symbols}
    assert ("Function", "add") in names
    assert ("Class", "Cart") in names
    assert ("Function", "total") in names
    assert any("App\\Models\\User" in i.name for i in parsed.imports)
    assert any(c.caller == "add" and c.callee == "helper" for c in parsed.calls)


def test_new_languages_index_end_to_end(tmp_path):
    from graphora.blast import blast_radius
    from graphora.embedded import EmbeddedGraphStore
    from graphora.indexer import index_repository

    repo = tmp_path / "poly"
    repo.mkdir()
    (repo / "lib.rs").write_text(RUST)
    (repo / "main.c").write_text(C)
    (repo / "cart.rb").write_text(RUBY)
    (repo / "Cart.php").write_text(PHP)
    store = EmbeddedGraphStore("poly", data_dir=tmp_path / "data")
    index_repository(repo, project="poly", store=store)
    stats = store.stats()
    assert stats["files"] == 4
    assert stats["functions"] >= 8
    radius = blast_radius(store, ["helper"])
    # helper exists in 4 languages; all definitions resolved with callers
    assert len(radius.symbols) == 4
    assert all(s.callers for s in radius.symbols)


def test_diff_symbol_extraction_covers_new_languages():
    from graphora.parser import extract_symbols_from_diff

    diff = (
        "--- a/lib.rs\n+++ b/lib.rs\n@@ -1 +1 @@\n"
        "+fn add(a: i32) -> i32 {\n"
        "--- a/cart.rb\n+++ b/cart.rb\n@@ -1 +1 @@\n"
        "+def total\n"
        "--- a/Cart.php\n+++ b/Cart.php\n@@ -1 +1 @@\n"
        "+function render($x) {\n"
    )
    symbols = extract_symbols_from_diff(diff)
    assert "add" in symbols and "total" in symbols and "render" in symbols
