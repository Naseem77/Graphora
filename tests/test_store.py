from app.graph import store
from app.parser.treesitter import parse_code_file


class FakeGraph:
    def __init__(self):
        self.queries = []
        self.file_count = 0

    def query(self, query, params=None):
        self.queries.append((query, params or {}))
        if "RETURN count(file)" in query:
            return type("Result", (), {"result_set": [[self.file_count]]})()


def test_write_code_graph_creates_files_symbols_and_imports(monkeypatch, tmp_path):
    fake_graph = FakeGraph()
    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": fake_graph)
    parsed = parse_code_file("app/example.py", "import os\nclass User:\n    pass\ndef helper():\n    pass\ndef login():\n    return helper()\n")

    count = store.write_code_graph("octo", "repo", [parsed])

    assert count == 1
    queries = "\n".join(query for query, _ in fake_graph.queries)
    assert "MERGE (file:File" in queries
    assert "MERGE (module:Module" in queries
    assert "MERGE (symbol:Class" in queries
    assert "MERGE (symbol:Function" in queries
    assert "CALLS" in queries
    assert "DEPENDS_ON" in queries
    assert "content_hash" in queries
    assert "signature_hash" in queries
    assert "stable_key" in queries

    file_params = next(params for query, params in fake_graph.queries if "MERGE (file:File" in query)
    assert file_params["id"] == "octo/repo:file:app/example.py"
    assert file_params["graph_scope"] == "main"
    assert file_params["symbol_count"] == 3

    symbol_params = next(
        params
        for query, params in fake_graph.queries
        if "MERGE (symbol:Function" in query and params["name"] == "login"
    )
    assert symbol_params["id"] == "octo/repo:symbol:app/example.py:Function:login"
    assert symbol_params["stable_key"] == "app/example.py:Function:login"


def test_write_code_graph_creates_doc_nodes(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": fake_graph)
    parsed = parse_code_file("README.md", "# Title\n\n## Usage\n")

    store.write_code_graph("octo", "repo", [parsed])

    queries = "\n".join(query for query, _ in fake_graph.queries)
    assert "MERGE (doc:DocPage" in queries
    assert "MERGE (section:DocSection" in queries


def test_delete_file_subgraph_removes_file_symbols_and_doc_sections(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": fake_graph)

    store.delete_file_subgraph("octo", "repo", "app/example.py")

    assert any("DETACH DELETE symbol" in query for query, _ in fake_graph.queries)
    assert any("DETACH DELETE section" in query for query, _ in fake_graph.queries)
    assert any("MATCH (file:File" in query for query, _ in fake_graph.queries)
    assert any("MATCH (doc:DocPage" in query for query, _ in fake_graph.queries)


def test_select_graph_uses_suffix(monkeypatch, tmp_path):
    selected = {}

    class FakeDB:
        def __init__(self, host, port):
            pass

        def select_graph(self, name):
            selected["name"] = name
            return FakeGraph()

    monkeypatch.setattr("falkordb.FalkorDB", FakeDB)

    graph = store._select_graph("octo", "repo", type("S", (), {"falkordb_host": "h", "falkordb_port": 1})(), "pr:7")

    assert isinstance(graph, FakeGraph)
    assert selected["name"] == "graph:octo:repo:pr:7"


def test_code_graph_has_files_checks_file_count(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": fake_graph)

    assert not store.code_graph_has_files("octo", "repo")

    fake_graph.file_count = 2

    assert store.code_graph_has_files("octo", "repo")


def test_code_graph_stats_aggregates_nodes_and_files(monkeypatch):
    class StatsGraph:
        def query(self, query, params=None):
            if "labels(n), count(n)" in query:
                rows = [[["File"], 2], [["Function"], 5]]
            elif "count(r)" in query:
                rows = [[7]]
            elif "file.path" in query:
                rows = [["a.py"], ["b.py"]]
            else:
                rows = []
            return type("Result", (), {"result_set": rows})()

    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": StatsGraph())

    stats = store.code_graph_stats("octo", "repo", suffix="pr:5")

    assert stats["node_counts"] == {"File": 2, "Function": 5}
    assert stats["node_total"] == 7
    assert stats["relationship_count"] == 7
    assert stats["files"] == ["a.py", "b.py"]


def test_module_candidate_paths_resolves_python_dotted_imports():
    assert "app/graph/store.py" in store._module_candidate_paths("app.graph.store")
    assert "app/provision/__init__.py" in store._module_candidate_paths("app.provision")


def test_module_candidate_paths_resolves_relative_path_imports():
    candidates = store._module_candidate_paths("./utils")
    assert "utils.ts" in candidates
    assert "utils.js" in candidates
    assert "utils/index.js" in candidates


def test_module_candidate_paths_empty_for_blank_import():
    assert store._module_candidate_paths("") == []


def test_write_code_graph_resolves_calls_across_files(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": fake_graph)

    caller = parse_code_file(
        "app/api.py",
        "from app.auth import login\n\ndef handler():\n    return login()\n",
    )
    callee = parse_code_file(
        "app/auth.py",
        "def login():\n    return True\n",
    )

    store.write_code_graph("octo", "repo", [caller, callee])

    call_queries = [(q, p) for q, p in fake_graph.queries if "CALLS" in q and "MERGE (caller" in q]
    assert len(call_queries) == 1
    query, params = call_queries[0]
    assert params["callee"] == "login"
    assert "app/auth.py" in params["import_paths"]
    assert "CASE WHEN callee.path = $path THEN 'same_file' ELSE 'import'" in query


def test_write_code_graph_writes_all_symbols_before_any_calls(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": fake_graph)

    caller = parse_code_file(
        "app/api.py",
        "from app.auth import login\n\ndef handler():\n    return login()\n",
    )
    callee = parse_code_file(
        "app/auth.py",
        "def login():\n    return True\n",
    )

    # caller listed first: if calls were resolved per-file (old behavior) the
    # callee's Function node would not exist yet when handler's CALLS edge is
    # written. The fix defers all CALLS writes to a second pass.
    store.write_code_graph("octo", "repo", [caller, callee])

    query_order = [q for q, _ in fake_graph.queries]
    last_symbol_index = max(i for i, q in enumerate(query_order) if "MERGE (symbol:Function" in q)
    first_calls_index = min(i for i, q in enumerate(query_order) if "MERGE (caller" in q)
    assert first_calls_index > last_symbol_index


def test_write_code_graph_reports_progress_per_file(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": fake_graph)
    progress = []

    parsed_a = parse_code_file("a.py", "def a():\n    pass\n")
    parsed_b = parse_code_file("b.py", "def b():\n    pass\n")

    store.write_code_graph(
        "octo",
        "repo",
        [parsed_a, parsed_b],
        on_progress=lambda stage, current, total, path: progress.append((stage, current, total, path)),
    )

    assert progress == [
        ("writing structural graph", 1, 2, "a.py"),
        ("writing structural graph", 2, 2, "b.py"),
    ]


def test_symbol_impact_collects_callers_callees_tests(monkeypatch):
    class ImpactGraph:
        def query(self, query, params=None):
            name = (params or {}).get("name")
            if name != "login":
                return type("R", (), {"result_set": []})()
            if "DEFINED_IN" in query:
                rows = [["Function", "app/auth.py", 10, "def login()"]]
            elif "caller:Function)-[:CALLS]" in query and "test" not in query:
                rows = [["handler", "app/api.py"]]
            elif "{name: $name})-[:CALLS]->(callee:Function)" in query:
                rows = [["hash_pw", "app/crypto.py"]]
            elif "test.name STARTS WITH" in query:
                rows = [["test_login", "tests/test_auth.py"]]
            else:
                rows = []
            return type("R", (), {"result_set": rows})()

    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings, suffix="main": ImpactGraph())

    impacts = store.symbol_impact("o", "r", ["login", "missing"])

    assert len(impacts) == 1
    impact = impacts[0]
    assert impact["name"] == "login"
    assert impact["definitions"][0]["path"] == "app/auth.py"
    assert impact["callers"] == [{"name": "handler", "path": "app/api.py"}]
    assert impact["callees"] == [{"name": "hash_pw", "path": "app/crypto.py"}]
    assert impact["tests"] == [{"name": "test_login", "path": "tests/test_auth.py"}]
