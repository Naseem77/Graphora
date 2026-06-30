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
