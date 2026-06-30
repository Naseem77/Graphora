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
    monkeypatch.setattr(chat, "get_kg", lambda owner, repo, settings=None: FakeKG())
    monkeypatch.setattr(chat, "settings_for_installation", lambda installation_id: None)
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


def _command_payload(body):
    return {
        "comment": {"user": {"type": "User"}, "body": body},
        "repository": {"owner": {"login": "octo"}, "name": "repo"},
        "issue": {"number": 1},
        "installation": {"id": 123},
    }


def test_help_command_posts_help(monkeypatch, tmp_path):
    posted = {}
    monkeypatch.setattr(chat, "settings_for_installation", lambda installation_id: None)
    monkeypatch.setattr(chat, "get_github_client", lambda installation_id, settings: "client")
    monkeypatch.setattr(
        chat,
        "post_issue_comment",
        lambda github_client, owner, repo, issue_number, body: posted.update(body=body),
    )

    result = chat.asyncio.run(chat.answer_comment(_command_payload("@graphreview help"), _settings(tmp_path)))

    assert "commands" in result
    assert "@graphreview review" in posted["body"]


def test_pause_command_sets_state(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(chat, "settings_for_installation", lambda installation_id: None)
    monkeypatch.setattr(chat, "get_github_client", lambda installation_id, settings: "client")
    monkeypatch.setattr(chat, "set_review_paused", lambda owner, repo, paused: calls.update(paused=paused))
    monkeypatch.setattr(
        chat,
        "post_issue_comment",
        lambda github_client, owner, repo, issue_number, body: calls.update(body=body),
    )

    chat.asyncio.run(chat.answer_comment(_command_payload("@graphreview pause"), _settings(tmp_path)))

    assert calls["paused"] is True
    assert "paused" in calls["body"].lower()


def test_review_command_runs_review(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(chat, "settings_for_installation", lambda installation_id: None)
    monkeypatch.setattr(chat, "get_github_client", lambda installation_id, settings: "client")
    monkeypatch.setattr(chat, "get_pr_diff", lambda client, owner, repo, number: "DIFF")

    async def fake_review(github_client, owner, repo, pr_number, diff, settings):
        calls.update(diff=diff, pr_number=pr_number)

    monkeypatch.setattr(chat, "review_and_post_pr", fake_review)

    result = chat.asyncio.run(chat.answer_comment(_command_payload("@graphreview review"), _settings(tmp_path)))

    assert result == "review"
    assert calls == {"diff": "DIFF", "pr_number": 1}
