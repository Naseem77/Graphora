from app.config import Settings
from app.review import chat


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


def test_answer_comment_ignores_bot_comments(tmp_path):
    payload = {"comment": {"user": {"type": "Bot"}, "body": "@graphreview hi"}}

    assert chat.asyncio.run(chat.answer_comment(payload, _settings(tmp_path))) is None


def test_answer_comment_posts_answer(monkeypatch, tmp_path):
    class FakeSession:
        def ask(self, question):
            assert question == "what changed?"
            return "answer"

    class FakeKG:
        def chat_session(self):
            return FakeSession()

    posted = {}
    monkeypatch.setattr(chat, "get_kg", lambda owner, repo: FakeKG())
    monkeypatch.setattr(chat, "get_github_client", lambda installation_id, settings: "client")
    monkeypatch.setattr(
        chat,
        "post_issue_comment",
        lambda github_client, owner, repo, issue_number, body: posted.update(body=body),
    )
    payload = {
        "comment": {"user": {"type": "User"}, "body": "@graphreview what changed?"},
        "repository": {"owner": {"login": "octo"}, "name": "repo"},
        "issue": {"number": 1},
        "installation": {"id": 123},
    }

    answer = chat.asyncio.run(chat.answer_comment(payload, _settings(tmp_path)))

    assert answer == "answer"
    assert posted["body"] == "**@graphreview** answer"
