from app.graph import builder


class FakeKG:
    def __init__(self):
        self.sources = None

    def process_sources(self, sources):
        self.sources = sources


def test_build_repo_graph_filters_supported_files(monkeypatch):
    fake = FakeKG()
    monkeypatch.setattr(builder, "get_kg", lambda owner, repo: fake)
    monkeypatch.setattr(builder, "create_source", lambda text: text)

    count = builder.build_repo_graph(
        "octo",
        "repo",
        [
            {"path": "src/a.py", "content": "def a():\n    pass"},
            {"path": "README.md", "content": "# docs"},
        ],
    )

    assert count == 1
    assert fake.sources is not None
    assert "Function: a" in fake.sources[0]
