from app.graph import pr_graph


class FakeKG:
    def __init__(self):
        self.sources = []

    def process_sources(self, sources):
        self.sources = sources


def test_build_pr_graph_uses_pr_suffix(monkeypatch, tmp_path):
    fake = FakeKG()
    calls = {}
    monkeypatch.setattr(pr_graph, "clear_code_graph", lambda owner, repo, settings, suffix: calls.update(clear=suffix))
    monkeypatch.setattr(
        pr_graph,
        "write_code_graph",
        lambda owner, repo, parsed_files, settings, suffix: calls.update(write=suffix),
    )
    monkeypatch.setattr(pr_graph, "get_kg", lambda owner, repo, suffix, settings: fake)
    monkeypatch.setattr(pr_graph, "create_source", lambda text: text)
    monkeypatch.setattr(pr_graph, "mark_graph_build", lambda owner, repo, graph_scope, status, source_count, settings: None)

    count = pr_graph.build_pr_graph(
        "octo",
        "repo",
        5,
        [{"path": "app.py", "content": "def hello():\n    pass\n"}],
    )

    assert count == 1
    assert calls == {"clear": "pr:5", "write": "pr:5"}
    assert fake.sources
