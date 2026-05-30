from app.config import Settings
from app.review import reviewer


class FakeMessage:
    content = "No meaningful issues found."


class FakeChoice:
    message = FakeMessage()


class FakeResponse:
    choices = [FakeChoice()]


def test_ask_llm_for_review(monkeypatch, tmp_path):
    calls = {}

    def fake_completion(**kwargs):
        calls.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(reviewer.litellm, "completion", fake_completion)
    settings = Settings(
        github_app_id="1",
        github_private_key_path=tmp_path / "key.pem",
        github_webhook_secret="secret",
        azure_api_key="key",
        azure_api_base="https://example.openai.azure.com",
        azure_api_version="2024-02-15-preview",
        review_model="azure/reviewer",
    )

    text = reviewer._ask_llm_for_review("+def changed():\n+    pass", "Context", settings)

    assert text == "No meaningful issues found."
    assert calls["model"] == "azure/reviewer"
    assert calls["api_base"] == "https://example.openai.azure.com"
