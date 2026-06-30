from app.graph import pr_graph


def test_build_pr_graph_uses_pr_suffix(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(pr_graph, "clear_code_graph", lambda owner, repo, settings, suffix: calls.update(clear=suffix))
    monkeypatch.setattr(
        pr_graph,
        "write_code_graph",
        lambda owner, repo, parsed_files, settings, suffix: calls.update(write=suffix),
    )
    monkeypatch.setattr(pr_graph, "mark_graph_build", lambda owner, repo, graph_scope, status, source_count, settings: None)

    count = pr_graph.build_pr_graph(
        "octo",
        "repo",
        5,
        [{"path": "app.py", "content": "def hello():\n    pass\n"}],
    )

    assert count == 1
    assert calls == {"clear": "pr:5", "write": "pr:5"}


def test_build_pr_graph_reports_progress(monkeypatch):
    progress = []
    monkeypatch.setattr(pr_graph, "clear_code_graph", lambda owner, repo, settings, suffix: None)
    monkeypatch.setattr(pr_graph, "write_code_graph", lambda owner, repo, parsed_files, settings, suffix: None)
    monkeypatch.setattr(pr_graph, "mark_graph_build", lambda owner, repo, graph_scope, status, source_count, settings: None)

    pr_graph.build_pr_graph(
        "octo",
        "repo",
        5,
        [{"path": "app.py", "content": "def hello():\n    pass\n"}],
        on_progress=lambda stage, current, total, path: progress.append((stage, current, total, path)),
    )

    assert progress == [
        ("parsing", 1, 1, "app.py"),
        ("writing structural graph", 1, 1, "app.py"),
        ("complete", 1, 1, ""),
    ]
