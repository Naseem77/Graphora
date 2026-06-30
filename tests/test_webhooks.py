import hashlib
import hmac
import asyncio

from app.config import get_settings
from app.github import webhooks


def test_verify_signature(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    body = b'{"ok":true}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    assert webhooks.verify_signature(body, signature)
    assert not webhooks.verify_signature(body, "sha256=bad")


def test_changed_paths_are_unique():
    commits = [
        {"added": ["a.py"], "modified": ["b.py"]},
        {"added": ["a.py"], "modified": ["c.py"]},
    ]

    assert webhooks._changed_paths_from_commits(commits) == ["a.py", "b.py", "c.py"]


def test_pull_request_diff_collects_patches():
    class File:
        filename = "app.py"
        patch = "+print('hi')"

    class PR:
        def get_files(self):
            return [File()]

    assert webhooks._pull_request_diff(PR()) == "diff --git a/app.py b/app.py\n+print('hi')"


def test_content_from_patch_returns_added_lines_only():
    patch = """@@ -0,0 +1,3 @@
+++ b/app.py
+import os
+def hello():
+    return os.getcwd()
-removed
 context
"""

    assert webhooks._content_from_patch(patch) == "import os\ndef hello():\n    return os.getcwd()"


def test_dispatch_skips_duplicate_delivery(monkeypatch):
    called = False

    async def handler(payload):
        nonlocal called
        called = True

    monkeypatch.setattr(webhooks, "handle_push", handler)
    monkeypatch.setattr(webhooks, "mark_delivery", lambda event, delivery_id: False)

    asyncio.run(webhooks.dispatch_webhook("push", {}, delivery_id="d1"))

    assert not called


def test_dispatch_queues_background_task(monkeypatch):
    class Tasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, func, *args):
            self.tasks.append((func, args))

    async def handler(payload):
        return None

    tasks = Tasks()
    monkeypatch.setattr(webhooks, "handle_push", handler)
    monkeypatch.setattr(webhooks, "mark_delivery", lambda event, delivery_id: True)

    asyncio.run(webhooks.dispatch_webhook("push", {"repository": {"full_name": "o/r"}}, tasks, "d1"))

    assert len(tasks.tasks) == 1


def test_dispatch_supports_workflow_run(monkeypatch):
    called = False

    async def handler(payload):
        nonlocal called
        called = payload["workflow_run"]["id"] == 123

    monkeypatch.setattr(webhooks, "handle_workflow_run", handler)
    monkeypatch.setattr(webhooks, "mark_delivery", lambda event, delivery_id: True)

    asyncio.run(
        webhooks.dispatch_webhook(
            "workflow_run",
            {"repository": {"full_name": "o/r"}, "workflow_run": {"id": 123}},
            delivery_id="d1",
        )
    )

    assert called


def test_ensure_main_graph_skips_existing_graph(monkeypatch):
    calls = []

    monkeypatch.setattr(webhooks, "code_graph_has_files", lambda owner, repo, settings=None: True)
    monkeypatch.setattr(webhooks, "fetch_repository_files", lambda *args: calls.append("fetch"))
    monkeypatch.setattr(webhooks, "build_repo_graph", lambda *args: calls.append("build"))

    result = asyncio.run(webhooks._ensure_main_graph("client", "octo", "repo", "base-sha"))

    assert result is None
    assert calls == []


def test_ensure_main_graph_bootstraps_missing_graph(monkeypatch):
    calls = []

    monkeypatch.setattr(webhooks, "code_graph_has_files", lambda owner, repo, settings=None: False)
    monkeypatch.setattr(
        webhooks,
        "fetch_repository_files",
        lambda client, owner, repo, ref: calls.append(("fetch", ref)) or [{"path": "app.py", "content": "def a(): pass"}],
    )
    monkeypatch.setattr(webhooks, "build_repo_graph", lambda owner, repo, files, on_progress=None, settings=None: calls.append(("build", files)) or 1)

    result = asyncio.run(webhooks._ensure_main_graph("client", "octo", "repo", "base-sha"))

    assert result == 1
    assert calls == [
        ("fetch", "base-sha"),
        ("build", [{"path": "app.py", "content": "def a(): pass"}]),
    ]


def test_pr_status_marker_and_body():
    assert webhooks._pr_status_marker(7, "abc") == "<!-- graphora-pr-status:7:abc -->"
    body = webhooks._pr_status_body("Building PR graph")

    assert "## Graphora Analysis Status" in body
    assert "Main repository graph" not in body
    assert "| PR overlay graph | **RUNNING** | - |" in body
    assert "| Review agent | **PENDING** | - |" in body
    assert "> Building PR graph" in body


def test_pr_status_body_shows_pr_progress():
    body = webhooks._pr_status_body("PR graph progress: 5 / 42 files - ingesting GraphRAG: `app/main.py`")

    assert "Main repository graph" not in body
    assert "| PR overlay graph | **RUNNING** | 5 / 42 files - ingesting GraphRAG: `app/main.py` |" in body
    assert "| Review agent | **PENDING** | - |" in body


def test_post_pr_status_comment_updates_marker(monkeypatch):
    calls = []

    monkeypatch.setattr(
        webhooks,
        "post_or_update_pr_comment",
        lambda client, owner, repo, pr_number, marker, body: calls.append((marker, body)),
    )

    asyncio.run(webhooks._post_pr_status_comment("client", "octo", "repo", 7, "abc", "Review complete"))

    assert calls == [
        (
            "<!-- graphora-pr-status:7:abc -->",
            "## Graphora Analysis Status\n\n"
            "| Step | Status | Progress |\n"
            "| --- | --- | --- |\n"
            "| PR overlay graph | **DONE** | complete |\n"
            "| Review agent | **DONE** | complete |\n\n"
            "> Review complete",
        )
    ]


def test_pr_status_body_shows_failed_analysis():
    body = webhooks._pr_status_body("Analysis failed: 403 Resource not accessible by integration")

    assert "Main repository graph" not in body
    assert "| PR overlay graph | **FAILED** | - |" in body
    assert "| Review agent | **SKIPPED** | - |" in body
    assert "> Analysis failed: 403 Resource not accessible by integration" in body


def test_log_progress_reporter_logs_main_graph(caplog):
    import logging

    reporter = webhooks._LogProgressReporter("octo", "repo", "Main graph", file_interval=5)
    with caplog.at_level(logging.INFO):
        reporter("parsing", 1, 10, "a.py")
        reporter("complete", 10, 10, "")

    messages = "\n".join(record.message for record in caplog.records)
    assert "Main graph build for octo/repo" in messages
    assert "Main graph build complete for octo/repo" in messages


def test_format_status_error_truncates_long_messages():
    message = webhooks._format_status_error(RuntimeError("x" * 600))

    assert len(message) == 500
    assert message.endswith("...")


def test_pr_progress_reporter_throttles_and_updates(monkeypatch):
    calls = []

    monkeypatch.setattr(
        webhooks,
        "_post_pr_status_comment_sync",
        lambda client, owner, repo, pr_number, marker, body: calls.append((marker, body)),
    )
    reporter = webhooks._PrProgressReporter(
        "client",
        "octo",
        "repo",
        7,
        "abc",
        "PR graph",
        min_interval_seconds=999,
        file_interval=5,
    )

    reporter("parsing", 1, 12, "a.py")
    reporter("parsing", 2, 12, "b.py")
    reporter("parsing", 6, 12, "f.py")
    reporter("complete", 12, 12, "")

    assert len(calls) == 3
    assert calls[0][0] == "<!-- graphora-pr-status:7:abc -->"
    assert "1 / 12 files - parsing: `a.py`" in calls[0][1]
    assert "6 / 12 files - parsing: `f.py`" in calls[1][1]
    assert "12 / 12 files - complete" in calls[2][1]
