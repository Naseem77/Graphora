from app.graph import store
from app.parser.treesitter import parse_code_file


class FakeGraph:
    def __init__(self):
        self.queries = []

    def query(self, query, params=None):
        self.queries.append((query, params or {}))


def test_write_code_graph_creates_files_symbols_and_imports(monkeypatch, tmp_path):
    fake_graph = FakeGraph()
    monkeypatch.setattr(store, "_select_graph", lambda owner, repo, settings: fake_graph)
    parsed = parse_code_file("app/example.py", "import os\nclass User:\n    pass\ndef login():\n    pass\n")

    count = store.write_code_graph("octo", "repo", [parsed])

    assert count == 1
    queries = "\n".join(query for query, _ in fake_graph.queries)
    assert "MERGE (file:File" in queries
    assert "MERGE (module:Module" in queries
    assert "MERGE (symbol:Class" in queries
    assert "MERGE (symbol:Function" in queries
