from app.graph import builder


def test_build_repo_graph_filters_supported_sources(monkeypatch):
    written = []
    monkeypatch.setattr(builder, "clear_code_graph", lambda owner, repo: None)
    monkeypatch.setattr(
        builder, "write_code_graph", lambda owner, repo, parsed_files: written.extend(parsed_files)
    )
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
    assert len(written) == 2


def test_build_repo_graph_reports_progress(monkeypatch):
    progress = []
    monkeypatch.setattr(builder, "clear_code_graph", lambda owner, repo: None)
    monkeypatch.setattr(builder, "write_code_graph", lambda owner, repo, parsed_files: len(parsed_files))
    monkeypatch.setattr(builder, "mark_graph_build", lambda owner, repo, graph_scope, status, source_count: None)

    builder.build_repo_graph(
        "octo",
        "repo",
        [{"path": "src/a.py", "content": "def a():\n    pass"}],
        on_progress=lambda stage, current, total, path: progress.append((stage, current, total, path)),
    )

    assert progress == [
        ("parsing", 1, 1, "src/a.py"),
        ("writing structural graph", 1, 1, "src/a.py"),
        ("complete", 1, 1, ""),
    ]
