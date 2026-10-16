"""Call resolution beyond Python: imports, types, and scopes in each language."""

from graphora.parser import AMBIGUOUS, EXTRACTED, parse_code_file
from graphora.resolve import records_for, resolve


def _resolve(files: dict[str, str]):
    symbols, records = [], []
    for path, source in files.items():
        file_symbols, record = records_for(parse_code_file(path, source))
        symbols.extend(file_symbols)
        records.append(record)
    result = resolve(symbols, records)
    by_key = {s.key: s for s in symbols}
    return {
        (by_key[e.a].qualname, by_key[e.b].path, by_key[e.b].qualname): (e.confidence, e.reason)
        for e in result.edges
    }


def _targets(edges, caller):
    return {(path, qual) for (a, path, qual) in edges if a == caller}


def test_java_import_field_type_and_implicit_this():
    edges = _resolve(
        {
            "src/app/repo/Repo.java": "package app.repo;\npublic class Repo { public int all() { return 1; } }\n",
            "src/other/Repo.java": "package other;\npublic class Repo { public int all() { return 2; } }\n",
            "src/app/Service.java": (
                "package app;\n"
                "import app.repo.Repo;\n"
                "public class Service {\n"
                "    private Repo repo;\n"
                "    int count() { return repo.all() + helper(); }\n"
                "    int helper() { return 0; }\n"
                "}\n"
            ),
        }
    )
    assert _targets(edges, "Service.count") == {
        ("src/app/repo/Repo.java", "Repo.all"),
        ("src/app/Service.java", "Service.helper"),
    }
    assert edges[("Service.count", "src/app/Service.java", "Service.helper")] == (EXTRACTED, "self")


def test_java_constructor_and_var_type():
    edges = _resolve(
        {
            "src/Point.java": "public class Point { Point() {} int sum() { return 1; } }\n",
            "src/Main.java": "public class Main { void run() { var p = new Point(); p.sum(); } }\n",
        }
    )
    assert _targets(edges, "Main.run") == {("src/Point.java", "Point.Point"), ("src/Point.java", "Point.sum")}


def test_rust_use_path_constructor_type_and_module_function():
    edges = _resolve(
        {
            "src/shapes/point.rs": (
                "pub struct Point { x: i32 }\n"
                "impl Point {\n"
                "    pub fn new(x: i32) -> Self { Point { x } }\n"
                "    pub fn norm(&self) -> i32 { self.x }\n"
                "}\n"
            ),
            "src/other.rs": "pub struct Point;\nimpl Point { pub fn norm(&self) -> i32 { 0 } }\n",
            "src/main.rs": (
                "use crate::shapes::point::Point;\n"
                "mod util { pub fn clamp(x: i32) -> i32 { x } }\n"
                "fn main() {\n"
                "    let p = Point::new(1);\n"
                "    p.norm();\n"
                "    util::clamp(2);\n"
                "}\n"
            ),
        }
    )
    assert _targets(edges, "main") == {
        ("src/shapes/point.rs", "Point.new"),
        ("src/shapes/point.rs", "Point.norm"),
        ("src/main.rs", "util.clamp"),
    }
    assert edges[("main", "src/main.rs", "util.clamp")] == (EXTRACTED, "namespace")


def test_rust_self_method_in_impl():
    edges = _resolve(
        {
            "src/a.rs": (
                "struct A;\n"
                "impl A {\n"
                "    fn run(&self) { self.step(); }\n"
                "    fn step(&self) {}\n"
                "}\n"
                "struct B;\nimpl B { fn step(&self) {} }\n"
            )
        }
    )
    assert _targets(edges, "A.run") == {("src/a.rs", "A.step")}


def test_c_include_prefers_the_matching_source_file():
    edges = _resolve(
        {
            "src/point.h": "int helper(int x);\n",
            "src/point.c": '#include "point.h"\nint helper(int x) { return x; }\n',
            "src/other.c": "int helper(int x) { return 0; }\n",
            "src/main.c": '#include "point.h"\nint main(void) { return helper(1); }\n',
        }
    )
    assert _targets(edges, "main") == {("src/point.c", "helper")}
    assert edges[("main", "src/point.c", "helper")] == (EXTRACTED, "import")


def test_cpp_namespace_qualified_call():
    edges = _resolve(
        {
            "src/geo.cpp": "namespace geo { double total(int n) { return n; } }\n",
            "src/fin.cpp": "namespace fin { double total(int n) { return 0; } }\n",
            "src/main.cpp": "int main() { return (int) geo::total(3); }\n",
        }
    )
    assert _targets(edges, "main") == {("src/geo.cpp", "geo.total")}


def test_ruby_require_relative_construction_and_typed_variable():
    edges = _resolve(
        {
            "lib/billing.rb": (
                "class Invoice\n"
                "  def initialize(total)\n    @total = total\n  end\n"
                "  def paid?\n    true\n  end\n"
                "end\n"
            ),
            "lib/other.rb": "class Receipt\n  def paid?\n    false\n  end\nend\n",
            "lib/app.rb": (
                "require_relative 'billing'\n"
                "def checkout\n"
                "  invoice = Invoice.new(10)\n"
                "  invoice.paid?\n"
                "end\n"
            ),
        }
    )
    assert _targets(edges, "checkout") == {
        ("lib/billing.rb", "Invoice.initialize"),
        ("lib/billing.rb", "Invoice.paid?"),
    }


def test_php_use_new_instance_and_static_call():
    edges = _resolve(
        {
            "src/Models/User.php": (
                "<?php\nnamespace App\\Models;\n"
                "class User {\n"
                "    public function save() { return 1; }\n"
                "    public static function find($id) { return new User(); }\n"
                "}\n"
            ),
            "src/Other/User.php": "<?php\nclass User { public function save() { return 0; } }\n",
            "src/Controller.php": (
                "<?php\nuse App\\Models\\User;\n"
                "function store() {\n"
                "    $u = new User();\n"
                "    $u->save();\n"
                "    return User::find(1);\n"
                "}\n"
            ),
        }
    )
    assert _targets(edges, "store") >= {
        ("src/Models/User.php", "User.save"),
        ("src/Models/User.php", "User.find"),
    }
    assert ("src/Other/User.php", "User.save") not in _targets(edges, "store")


def test_typescript_typed_parameter_and_class_field():
    edges = _resolve(
        {
            "src/graph.ts": "export class Graph { query(q: string) { return q; } }\n",
            "src/other.ts": "export class Cache { query(q: string) { return q; } }\n",
            "src/app.ts": (
                "import { Graph } from './graph';\n"
                "export class Service {\n"
                "  private graph: Graph;\n"
                "  run(q: string) { return this.graph.query(q); }\n"
                "}\n"
                "export function direct(g: Graph) { return g.query('x'); }\n"
            ),
        }
    )
    assert _targets(edges, "Service.run") == {("src/graph.ts", "Graph.query")}
    assert _targets(edges, "direct") == {("src/graph.ts", "Graph.query")}


def test_javascript_object_literal_methods():
    edges = _resolve(
        {
            "src/api.js": (
                "const api = {\n"
                "  load() { return 1; },\n"
                "  save: function () { return 2; },\n"
                "};\n"
                "export function boot() { api.load(); api.save(); }\n"
            ),
            "src/other.js": "const store = { load() { return 0; } };\n",
        }
    )
    assert _targets(edges, "boot") == {("src/api.js", "api.load"), ("src/api.js", "api.save")}


def test_go_typed_variables_and_receivers():
    edges = _resolve(
        {
            "shapes/rect.go": (
                "package shapes\n"
                "type Rect struct { W int }\n"
                "func (r *Rect) Area() int { return r.W }\n"
            ),
            "shapes/circle.go": "package shapes\ntype Circle struct{}\nfunc (c Circle) Area() int { return 0 }\n",
            "shapes/use.go": (
                "package shapes\n"
                "func Total(r *Rect) int {\n"
                "    x := &Rect{W: 1}\n"
                "    var y Circle\n"
                "    return r.Area() + x.Area() + y.Area()\n"
                "}\n"
            ),
        }
    )
    assert _targets(edges, "Total") == {("shapes/rect.go", "Rect.Area"), ("shapes/circle.go", "Circle.Area")}


def test_unknown_receiver_in_other_language_stays_ambiguous():
    edges = _resolve(
        {
            "src/A.java": "class A { void go() {} }\n",
            "src/B.java": "class B { void go() {} }\n",
            "src/Main.java": "class Main { void run(Object o) { thing().go(); } Object thing() { return null; } }\n",
        }
    )
    assert {edges[("Main.run", p, q)][0] for p, q in _targets(edges, "Main.run") if q.endswith("go")} == {AMBIGUOUS}


def test_rust_crate_name_imports_external_types_and_inline_tests():
    from graphora.embedded import EmbeddedGraphStore
    from graphora.impact import what_could_break
    from graphora.indexer import index_repository

    files = {
        "Cargo.toml": '[package]\nname = "text-to-cypher"\nversion = "0.1.0"\n',
        "src/lib.rs": (
            "pub mod client;\n"
            "pub use client::TextToCypherClient;\n"
        ),
        "src/client.rs": (
            "pub struct TextToCypherClient { url: String }\n"
            "impl TextToCypherClient {\n"
            "    pub fn new(url: &str) -> Self { TextToCypherClient { url: String::new() } }\n"
            "}\n"
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    use super::*;\n"
            "    #[test]\n"
            "    fn test_creation() { TextToCypherClient::new(\"x\"); }\n"
            "}\n"
        ),
        "src/runtime.rs": "pub struct Engine;\nimpl Engine { pub fn new() -> Self { Engine } }\n",
        "examples/usage.rs": (
            "use text_to_cypher::TextToCypherClient;\n"
            "fn main() {\n"
            "    let c = TextToCypherClient::new(\"u\");\n"
            "    let s = String::new();\n"
            "    let r = Runtime::new();\n"
            "}\n"
        ),
    }
    edges = _resolve({k: v for k, v in files.items() if k.endswith(".rs") or k == "Cargo.toml"})
    assert _targets(edges, "main") == {("src/client.rs", "TextToCypherClient.new")}
    assert edges[("main", "src/client.rs", "TextToCypherClient.new")][0] == EXTRACTED

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        store = EmbeddedGraphStore("rust", data_dir=Path(tmp) / "data")
        index_repository(root, project="rust", store=store)
        result = what_could_break(store, ["TextToCypherClient.new"], depth=1)
        assert {b.caller["qualname"] for b in result.will_break} == {"main"}
        assert {b.caller["qualname"] for b in result.tests} == {"tests.test_creation"}
        assert result.might_break == []


def test_capitalized_types_outside_the_repo_are_external():
    edges = _resolve(
        {
            "src/util.js": "export class Stats { max(a) { return a; } keys(o) { return o; } }\n",
            "src/app.js": "export function run(o) { Math.max(1, 2); Object.keys(o); }\n",
            "src/Sorter.java": "class Sorter { void sort(Object x) {} }\n",
            "src/Main.java": "class Main { void run(java.util.List<String> l) { Collections.sort(l); } }\n",
            "app/stats.py": "class Stats:\n    def home(self):\n        return 1\n",
            "app/main.py": "def run():\n    return Path.home()\n",
        }
    )
    assert _targets(edges, "run") == set()
    assert _targets(edges, "Main.run") == set()
