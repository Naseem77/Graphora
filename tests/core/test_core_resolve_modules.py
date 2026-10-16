"""Import/module resolution forms not covered elsewhere, one per language."""

from graphora.parser import EXTRACTED, parse_code_file
from graphora.resolve import records_for, resolve


def _resolve(files: dict[str, str]):
    symbols, records = [], []
    for path, source in files.items():
        file_symbols, record = records_for(parse_code_file(path, source))
        symbols.extend(file_symbols)
        records.append(record)
    result = resolve(symbols, records)
    by_key = {s.key: s for s in symbols}
    edges = {
        (by_key[e.a].qualname, by_key[e.b].path, by_key[e.b].qualname): (e.confidence, e.reason)
        for e in result.edges
    }
    return edges, result


def _targets(edges, caller):
    return {(path, qual) for (a, path, qual) in edges if a == caller}


def test_commonjs_require_whole_module_and_destructured():
    edges, _ = _resolve(
        {
            "lib/util.js": "function fmt(x) { return x; }\nfunction slug(x) { return x; }\nmodule.exports = { fmt, slug };\n",
            "lib/other.js": "function fmt(x) { return 0; }\n",
            "app.js": (
                "const util = require('./lib/util');\n"
                "const { slug, fmt: format } = require('./lib/util');\n"
                "function run() { util.fmt(1); slug(2); format(3); }\n"
            ),
        }
    )
    assert _targets(edges, "run") == {("lib/util.js", "fmt"), ("lib/util.js", "slug")}
    assert {edges[("run", p, q)][0] for p, q in _targets(edges, "run")} == {EXTRACTED}


def test_typescript_path_alias():
    edges, _ = _resolve(
        {
            "src/lib/utils.ts": "export function cn(...a: string[]) { return a.join(' '); }\n",
            "src/other/utils.ts": "export function cn(x: string) { return x; }\n",
            "src/components/Button.tsx": (
                "import { cn } from '@/lib/utils';\nexport function Button() { return cn('a'); }\n"
            ),
        }
    )
    assert _targets(edges, "Button") == {("src/lib/utils.ts", "cn")}


def test_javascript_index_file_and_extensionless_imports():
    edges, _ = _resolve(
        {
            "src/api/index.js": "export function load() { return 1; }\n",
            "src/main.js": "import { load } from './api';\nexport function boot() { return load(); }\n",
        }
    )
    assert _targets(edges, "boot") == {("src/api/index.js", "load")}


def test_rust_use_lists_aliases_wildcards_and_relative_modules():
    edges, _ = _resolve(
        {
            "Cargo.toml": '[package]\nname = "geo"\n',
            "src/lib.rs": "pub mod shapes;\npub mod util;\n",
            "src/shapes/mod.rs": "pub mod circle;\npub fn area() -> f64 { 0.0 }\n",
            "src/shapes/circle.rs": (
                "use super::area;\n"
                "pub fn circle_area() -> f64 { area() }\n"
            ),
            "src/util.rs": "pub fn clamp(x: f64) -> f64 { x }\npub fn round(x: f64) -> f64 { x }\n",
            "src/main.rs": (
                "use geo::util::{clamp as limit, round};\n"
                "use geo::shapes::*;\n"
                "fn main() { limit(1.0); round(2.0); area(); }\n"
            ),
        }
    )
    assert _targets(edges, "circle_area") == {("src/shapes/mod.rs", "area")}
    assert _targets(edges, "main") == {
        ("src/util.rs", "clamp"),
        ("src/util.rs", "round"),
        ("src/shapes/mod.rs", "area"),
    }


def test_rust_external_crates_are_external():
    edges, result = _resolve(
        {
            "Cargo.toml": '[package]\nname = "app"\n',
            "src/json.rs": "pub fn to_string() -> String { String::new() }\n",
            "src/main.rs": "use serde_json::to_string;\nfn main() { to_string(); }\n",
        }
    )
    assert _targets(edges, "main") == set()
    assert result.external >= 1


def test_java_static_and_wildcard_imports():
    edges, _ = _resolve(
        {
            "src/app/util/Strings.java": "package app.util;\npublic class Strings { public static String trim(String s) { return s; } }\n",
            "src/app/model/User.java": "package app.model;\npublic class User { public String name() { return \"\"; } }\n",
            "src/app/Main.java": (
                "package app;\n"
                "import static app.util.Strings.trim;\n"
                "import app.model.*;\n"
                "class Main { void run(User u) { trim(u.name()); } }\n"
            ),
        }
    )
    assert _targets(edges, "Main.run") == {
        ("src/app/util/Strings.java", "Strings.trim"),
        ("src/app/model/User.java", "User.name"),
    }


def test_ruby_require_of_a_library_path():
    edges, _ = _resolve(
        {
            "lib/billing/invoice.rb": "class Invoice\n  def total\n    1\n  end\nend\n",
            "app.rb": "require 'billing/invoice'\ndef run\n  Invoice.new.total\nend\n",
        }
    )
    assert ("lib/billing/invoice.rb", "Invoice.total") in _targets(edges, "run")


def test_php_namespace_resolves_with_a_shorter_psr4_path():
    edges, _ = _resolve(
        {
            "src/Models/Order.php": "<?php\nnamespace App\\Models;\nclass Order { public function total() { return 1; } }\n",
            "src/Http/Controller.php": (
                "<?php\nuse App\\Models\\Order;\n"
                "function show() { $o = new Order(); return $o->total(); }\n"
            ),
        }
    )
    assert ("src/Models/Order.php", "Order.total") in _targets(edges, "show")


def test_go_module_root_package_import():
    edges, _ = _resolve(
        {
            "go.mod": "module example.com/tool\n",
            "util.go": "package tool\nfunc Helper() int { return 1 }\n",
            "cmd/run/main.go": 'package main\nimport tool "example.com/tool"\nfunc main() { tool.Helper() }\n',
        }
    )
    assert _targets(edges, "main") == {("util.go", "Helper")}
