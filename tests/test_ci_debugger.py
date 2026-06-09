from app.ci import debugger
from app.config import Settings


class FakeMessage:
    content = "Likely cause: app/refunds.py changed billing behavior."


class FakeChoice:
    message = FakeMessage()


class FakeResponse:
    choices = [FakeChoice()]


def _settings(tmp_path):
    return Settings(
        github_app_id="1",
        github_private_key_path=tmp_path / "key.pem",
        github_webhook_secret="secret",
        azure_api_key="key",
        azure_api_base="https://example.openai.azure.com",
        azure_api_version="2024-02-15-preview",
        review_model="azure/reviewer",
    )


def test_ask_llm_for_ci_debug_uses_graph_evidence(monkeypatch, tmp_path):
    calls = {}

    def fake_completion(**kwargs):
        calls.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(debugger.litellm, "completion", fake_completion)
    evidence = debugger.GraphEvidence(
        changed_files=["app/refunds.py"],
        failed_tests=["tests/test_billing.py::test_refund_status"],
        failed_paths=["tests/test_billing.py"],
        dependency_paths=["tests/test_billing.py -> app/billing/client.py -> app/refunds.py"],
        related_symbols=["app/refunds.py:Function:refund"],
    )

    text = debugger._ask_llm_for_ci_debug(
        {"name": "tests", "conclusion": "failure", "head_sha": "abc"},
        "FAILED tests/test_billing.py::test_refund_status",
        evidence,
        _settings(tmp_path),
    )

    assert text.startswith("Likely cause")
    prompt = calls["messages"][0]["content"]
    assert "CI debugging agent" in prompt
    assert "tests/test_billing.py -> app/billing/client.py -> app/refunds.py" in prompt


def test_pull_request_changed_file_names_skips_removed_files():
    class File:
        def __init__(self, filename, status):
            self.filename = filename
            self.status = status

    class PR:
        def get_files(self):
            return [File("app/a.py", "modified"), File("app/deleted.py", "removed")]

    assert debugger._pull_request_changed_file_names(PR()) == ["app/a.py"]


def test_ci_debug_marker_is_stable():
    assert debugger._ci_debug_marker(123, "abc") == "<!-- graphora-ci-debug:123:abc -->"
