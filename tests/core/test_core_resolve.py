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
    # `items.append(x)` may well be list.append, not the repo's Buffer.append.
    edges, _ = _resolve(
        {
            "buf.py": "class Buffer:\n    def append(self, x):\n        return x\n",
            "use.py": "def collect(items):\n    items.append(1)\n",
        }
    )
    assert edges[("collect", "buf.py", "Buffer.append")][:2] == (AMBIGUOUS, 1)


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
