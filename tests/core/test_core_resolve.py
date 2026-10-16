"""Call resolution: each call links to the definition the source actually names."""

from graphora.parser import AMBIGUOUS, EXTRACTED, INFERRED, parse_code_file
from graphora.resolve import MAX_CANDIDATES, records_for, resolve


def _resolve(files: dict[str, str]):
    symbols, records = [], []
    for path, source in files.items():
        file_symbols, record = records_for(parse_code_file(path, source))
        symbols.extend(file_symbols)
        records.append(record)
    result = resolve(symbols, records)
    by_key = {s.key: s for s in symbols}
    edges = {
        (by_key[e.a].qualname, by_key[e.b].path, by_key[e.b].qualname): (e.confidence, e.candidates, e.reason)
        for e in result.edges
    }
    return edges, result


def _targets(edges, caller):
    return {(path, qual) for (a, path, qual) in edges if a == caller}


def test_import_picks_the_named_module_not_every_same_name_function():
    edges, _ = _resolve(
        {
            "src/app/cli.py": "def main():\n    return 0\n",
            "scripts/build.py": "def main():\n    return 1\n",
            "tools/gen.py": "def main():\n    return 2\n",
            "tests/test_cli.py": "from app.cli import main\n\ndef test_main():\n    assert main() == 0\n",
        }
    )
    assert _targets(edges, "test_main") == {("src/app/cli.py", "main")}
    assert edges[("test_main", "src/app/cli.py", "main")] == (EXTRACTED, 1, "import")


def test_src_layout_import_resolves():
    edges, _ = _resolve(
        {
            "src/shiftory/evidence/builder.py": "def build_evidence():\n    return 1\n",
            "tests/integration/test_scopes.py": (
                "from shiftory.evidence.builder import build_evidence\n\n"
                "def test_it():\n    build_evidence()\n"
            ),
        }
    )
    assert edges[("test_it", "src/shiftory/evidence/builder.py", "build_evidence")][0] == EXTRACTED


def test_external_import_creates_no_edge():
    edges, result = _resolve(
        {
            "pkg/paths.py": "def join(a, b):\n    return a + b\n",
            "pkg/use.py": "import os\n\ndef run():\n    return os.path.join('a', 'b')\n",
        }
    )
    assert _targets(edges, "run") == set()
    assert result.external == 1


def test_self_call_resolves_to_own_class_method():
    edges, _ = _resolve(
        {
            "a.py": (
                "class A:\n"
                "    def save(self):\n        return 1\n"
                "    def run(self):\n"
                "        def inner():\n            return self.save()\n"
                "        return self.save()\n"
                "class B:\n"
                "    def save(self):\n        return 2\n"
            )
        }
    )
    assert _targets(edges, "A.run") == {("a.py", "A.save")}
    assert _targets(edges, "A.run.inner") == {("a.py", "A.save")}
    assert edges[("A.run", "a.py", "A.save")][0] == EXTRACTED


def test_unknown_receiver_lists_every_candidate_as_ambiguous():
    edges, _ = _resolve(
        {
            "db.py": "class Db:\n    def save(self):\n        return 1\n",
            "files.py": "class FileStore:\n    def save(self):\n        return 2\n",
            "use.py": "def persist(store):\n    store.save()\n",
        }
    )
    assert _targets(edges, "persist") == {("db.py", "Db.save"), ("files.py", "FileStore.save")}
    assert {edges[("persist", p, q)][:2] for p, q in _targets(edges, "persist")} == {(AMBIGUOUS, 2)}


def test_unknown_receiver_with_one_candidate_is_still_ambiguous():
    # `items.stage(x)` might be the repo's Buffer.stage, or a library object's method.
    edges, _ = _resolve(
        {
            "buf.py": "class Buffer:\n    def stage(self, x):\n        return x\n",
            "use.py": "def collect(items):\n    items.stage(1)\n",
        }
    )
    assert edges[("collect", "buf.py", "Buffer.stage")][:2] == (AMBIGUOUS, 1)


def test_bare_call_never_matches_a_method_in_python():
    edges, _ = _resolve(
        {
            "a.py": "class A:\n    def helper(self):\n        return 1\n",
            "b.py": "def run():\n    return helper()\n",
        }
    )
    assert _targets(edges, "run") == set()


def test_unique_bare_name_without_import_is_inferred():
    edges, _ = _resolve(
        {
            "lib.py": "def compute(x):\n    return x\n",
            "app.py": "def handler():\n    return compute(1)\n",
        }
    )
    assert edges[("handler", "lib.py", "compute")] == (INFERRED, 1, "name")


def test_construction_links_to_constructor():
    edges, _ = _resolve(
        {
            "w.py": "class Widget:\n    def __init__(self):\n        self.x = 1\n",
            "app.py": "from w import Widget\n\ndef make():\n    return Widget()\n",
        }
    )
    assert _targets(edges, "make") == {("w.py", "Widget.__init__")}


def test_same_name_methods_in_one_file_stay_separate():
    edges, _ = _resolve(
        {
            "errors.py": (
                "class A(Exception):\n    def __init__(self):\n        pass\n"
                "class B(Exception):\n    def __init__(self):\n        pass\n"
            ),
            "use.py": "from errors import B\n\ndef fail():\n    raise B()\n",
        }
    )
    assert _targets(edges, "fail") == {("errors.py", "B.__init__")}


def test_redefined_function_gets_its_own_key():
    symbols, _ = records_for(
        parse_code_file("c.py", "if X:\n    def compat():\n        return 1\nelse:\n    def compat():\n        return 2\n")
    )
    assert [s.key for s in symbols] == ["c.py:Function:compat", "c.py:Function:compat#2"]


def test_submodule_import_then_attribute_call():
    edges, _ = _resolve(
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def slug(s):\n    return s\n",
            "app.py": "from pkg import util\n\ndef run():\n    return util.slug('a')\n",
        }
    )
    assert edges[("run", "pkg/util.py", "slug")][0] == EXTRACTED


def test_relative_import():
    edges, _ = _resolve(
        {
            "pkg/core.py": "def compute():\n    return 1\n",
            "pkg/api.py": "from .core import compute\n\ndef handler():\n    return compute()\n",
            "other/core.py": "def compute():\n    return 2\n",
        }
    )
    assert _targets(edges, "handler") == {("pkg/core.py", "compute")}


def test_javascript_relative_imports():
    edges, _ = _resolve(
        {
            "src/util.ts": "export function fmt(x: string) { return x; }\nexport default function main() {}\n",
            "src/other/util.ts": "export function fmt(x: string) { return x; }\n",
            "src/app.ts": (
                "import run, { fmt as format } from './util';\n"
                "export function render() { format('a'); run(); }\n"
            ),
        }
    )
    assert _targets(edges, "render") == {("src/util.ts", "fmt"), ("src/util.ts", "main")}


def test_go_same_package_and_package_import():
    edges, _ = _resolve(
        {
            "go.mod": "module example.com/proj\n",
            "shapes/area.go": "package shapes\nfunc Area() int { return helper() }\n",
            "shapes/helper.go": "package shapes\nfunc helper() int { return 1 }\n",
            "other/helper.go": "package other\nfunc helper() int { return 2 }\n",
            "cmd/main.go": 'package main\nimport "example.com/proj/shapes"\nfunc main() { shapes.Area() }\n',
        }
    )
    assert _targets(edges, "Area") == {("shapes/helper.go", "helper")}
    assert _targets(edges, "main") == {("shapes/area.go", "Area")}


def test_too_many_candidates_are_not_fanned_out():
    files = {f"m{i}.py": "class K:\n    def run(self):\n        return 1\n" for i in range(MAX_CANDIDATES + 1)}
    files["use.py"] = "def go(x):\n    x.run()\n"
    edges, result = _resolve(files)
    assert _targets(edges, "go") == set()
    assert result.too_ambiguous == 1


def test_file_import_edges():
    _, result = _resolve(
        {
            "pkg/core.py": "def compute():\n    return 1\n",
            "pkg/api.py": "from pkg.core import compute\n",
        }
    )
    assert ("pkg/api.py", "pkg/core.py") in result.file_imports


def test_production_code_does_not_resolve_to_test_doubles():
    edges, _ = _resolve(
        {
            "app/provider.py": "class Provider:\n    def enrich(self):\n        return 1\n",
            "app/run.py": "def run(p):\n    return p.enrich()\n",
            "tests/test_run.py": "class FakeProvider:\n    def enrich(self):\n        return 0\n\ndef test_run():\n    FakeProvider().enrich()\n",
        }
    )
    assert _targets(edges, "run") == {("app/provider.py", "Provider.enrich")}
    assert _targets(edges, "test_run") == {
        ("tests/test_run.py", "FakeProvider"),  # the construction
        ("tests/test_run.py", "FakeProvider.enrich"),
    }


def test_variable_types_pick_the_right_twin_class():
    files = {
        "falkordb/falkordb.py": "class FalkorDB:\n    def select_graph(self, name):\n        return name\n",
        "falkordb/asyncio/falkordb.py": "class FalkorDB:\n    def select_graph(self, name):\n        return name\n",
        "falkordb/__init__.py": "from .falkordb import FalkorDB\n",
        "falkordb/asyncio/__init__.py": "from .falkordb import FalkorDB\n",
        "tests/test_async.py": (
            "from falkordb.asyncio import FalkorDB\n\n"
            "def test_select():\n"
            "    db = FalkorDB()\n"
            "    db.select_graph('g')\n"
        ),
        "app/service.py": (
            "from falkordb import FalkorDB\n\n"
            "class Service:\n"
            "    def __init__(self):\n"
            "        self.db = FalkorDB()\n"
            "    def graph(self, name):\n"
            "        return self.db.select_graph(name)\n"
            "def typed(db: FalkorDB):\n"
            "    return db.select_graph('x')\n"
        ),
    }
    edges, _ = _resolve(files)
    assert _targets(edges, "test_select") >= {("falkordb/asyncio/falkordb.py", "FalkorDB.select_graph")}
    assert ("falkordb/falkordb.py", "FalkorDB.select_graph") not in _targets(edges, "test_select")
    assert _targets(edges, "Service.graph") == {("falkordb/falkordb.py", "FalkorDB.select_graph")}
    assert _targets(edges, "typed") == {("falkordb/falkordb.py", "FalkorDB.select_graph")}
    assert edges[("typed", "falkordb/falkordb.py", "FalkorDB.select_graph")][0] == EXTRACTED


def test_javascript_reexport_through_index():
    edges, _ = _resolve(
        {
            "src/lib/format.ts": "export function fmt(x: string) { return x; }\n",
            "src/lib/index.ts": "export { fmt } from './format';\nexport * from './other';\n",
            "src/lib/other.ts": "export function slug(x: string) { return x; }\n",
            "src/fmt2.ts": "export function fmt(x: string) { return x; }\n",
            "src/app.ts": "import { fmt, slug } from './lib';\nexport function render() { fmt('a'); slug('b'); }\n",
        }
    )
    assert _targets(edges, "render") == {("src/lib/format.ts", "fmt"), ("src/lib/other.ts", "slug")}


def test_call_results_resolve_through_declared_return_types():
    edges, _ = _resolve(
        {
            "falkordb/graph.py": "class Graph:\n    def query(self, q):\n        return q\n",
            "falkordb/asyncio/graph.py": "class AsyncGraph:\n    def query(self, q):\n        return q\n",
            "falkordb/db.py": (
                "from falkordb.graph import Graph\n\n"
                "class FalkorDB:\n"
                "    def select_graph(self, name) -> Graph:\n"
                "        return Graph()\n"
            ),
            "app.py": (
                "from falkordb.db import FalkorDB\n\n"
                "def run():\n"
                "    db = FalkorDB()\n"
                "    graph = db.select_graph('g')\n"
                "    graph.query('a')\n"
                "    db.select_graph('h').query('b')\n"
            ),
        }
    )
    assert _targets(edges, "run") == {
        ("falkordb/db.py", "FalkorDB"),
        ("falkordb/db.py", "FalkorDB.select_graph"),
        ("falkordb/graph.py", "Graph.query"),
    }
    assert edges[("run", "falkordb/graph.py", "Graph.query")][::2] == (EXTRACTED, "returns")


def test_union_typed_receiver_lists_both_types():
    edges, _ = _resolve(
        {
            "stores.py": (
                "class FalkorStore:\n    def find(self, n):\n        return n\n"
                "class JsonStore:\n    def find(self, n):\n        return n\n"
                "class Other:\n    def find(self, n):\n        return n\n"
            ),
            "use.py": "from stores import FalkorStore, JsonStore\n\ndef lookup(store: FalkorStore | JsonStore):\n    return store.find('x')\n",
        }
    )
    assert _targets(edges, "lookup") == {("stores.py", "FalkorStore.find"), ("stores.py", "JsonStore.find")}
    assert edges[("lookup", "stores.py", "JsonStore.find")][::2] == (AMBIGUOUS, "union")


def test_from_dot_import_module_then_call():
    edges, _ = _resolve(
        {
            "pkg/__init__.py": "",
            "pkg/mod.py": "def f():\n    return 1\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/deep.py": "from .. import mod\n\ndef g():\n    return mod.f()\n",
            "pkg/main.py": "from . import mod\n\ndef run():\n    return mod.f()\n",
        }
    )
    assert edges[("run", "pkg/mod.py", "f")][0] == EXTRACTED
    assert edges[("g", "pkg/mod.py", "f")][0] == EXTRACTED


def test_relative_import_beyond_the_root_resolves_nowhere():
    edges, _ = _resolve(
        {"x.py": "def helper():\n    return 1\n", "a.py": "from ....x import helper\n\ndef run():\n    return helper()\n"}
    )
    assert _targets(edges, "run") == set()


def test_stdlib_and_package_internal_names_do_not_capture_imports():
    edges, result = _resolve(
        {
            "app/__init__.py": "",
            "app/handlers/__init__.py": "",
            "app/handlers/logging.py": "def info(m):\n    return m\n",
            "app/handlers/requests.py": "def get(u):\n    return u\n",
            "app/x.py": "import logging\nimport requests\n\ndef run():\n    logging.info('a')\n    requests.get('u')\n",
            "app/y.py": "from app.handlers import logging\n\ndef ok():\n    logging.info('b')\n",
        }
    )
    assert _targets(edges, "run") == set()
    assert result.external == 2
    assert _targets(edges, "ok") == {("app/handlers/logging.py", "info")}


def test_go_imports_resolve_through_go_mod_only():
    edges, _ = _resolve(
        {
            "go.mod": "module example.com/proj\n\ngo 1.22\n",
            "internal/json/json.go": "package json\nfunc Marshal(v any) []byte { return nil }\n",
            "pkg/errors/errors.go": "package errors\nfunc New(s string) error { return nil }\n",
            "cmd/main.go": (
                'package main\nimport (\n  "encoding/json"\n  "github.com/pkg/errors"\n'
                '  myjson "example.com/proj/internal/json"\n)\n'
                "func main() { json.Marshal(1); errors.New(\"x\"); myjson.Marshal(2) }\n"
            ),
        }
    )
    assert _targets(edges, "main") == {("internal/json/json.go", "Marshal")}


def test_go_without_go_mod_needs_two_matching_path_segments():
    edges, _ = _resolve(
        {
            "internal/json/json.go": "package json\nfunc Marshal(v any) []byte { return nil }\n",
            "cmd/main.go": 'package main\nimport "encoding/json"\nfunc main() { json.Marshal(1) }\n',
        }
    )
    assert _targets(edges, "main") == set()


def test_function_local_and_try_except_imports():
    edges, _ = _resolve(
        {
            "a.py": "def helper():\n    return 1\n",
            "b.py": "def helper():\n    return 2\n",
            "c.py": "def helper():\n    return 3\n",
            "fast.py": (
                "try:\n    from a import helper\nexcept ImportError:\n    from b import helper\n\n"
                "def run():\n    return helper()\n"
                "def local():\n    from c import helper\n    return helper()\n"
            ),
        }
    )
    assert _targets(edges, "run") == {("a.py", "helper"), ("b.py", "helper")}
    assert {edges[("run", p, q)][0] for p, q in _targets(edges, "run")} == {AMBIGUOUS}
    assert _targets(edges, "local") == {("c.py", "helper")}
    assert edges[("local", "c.py", "helper")][0] == EXTRACTED
