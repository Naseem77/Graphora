import hashlib
import hmac

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
