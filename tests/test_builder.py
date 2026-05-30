from app.graph import builder


class FakeKG:
    def __init__(self):
        self.sources = None

    def process_sources(self, sources):
        self.sources = sources


def test_build_repo_graph_filters_supported_sources(monkeypatch):
    fake = FakeKG()
    monkeypatch.setattr(builder, "get_kg", lambda owner, repo: fake)
    monkeypatch.setattr(builder, "create_source", lambda text: text)
    monkeypatch.setattr(builder, "clear_code_graph", lambda owner, repo: None)
    monkeypatch.setattr(builder, "write_code_graph", lambda owner, repo, parsed_files: len(parsed_files))
    monkeypatch.setattr(builder, "mark_graph_build", lambda owner, repo, graph_scope, status, source_count: None)

    count = builder.build_repo_graph(
        "octo",
        "repo",
        [
            {"path": "src/a.py", "content": "def a():\n    pass"},
            {"path": "README.md", "content": "# docs"},
        ],
    )

    assert count == 2
    assert fake.sources is not None
    assert "Function: a" in fake.sources[0]
    assert "Doc sections:" in fake.sources[1]
