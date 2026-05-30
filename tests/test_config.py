from app.config import Settings


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("FALKORDB_PORT", "6380")
    monkeypatch.setenv("AZURE_API_KEY", "azure-key")
    monkeypatch.setenv("AZURE_API_BASE", "https://example.openai.azure.com")
    monkeypatch.setenv("REVIEW_MODEL", "azure/reviewer")

    settings = Settings.from_env()

    assert settings.github_app_id == "123"
    assert settings.github_webhook_secret == "secret"
    assert settings.falkordb_port == 6380
    assert settings.azure_api_key == "azure-key"
    assert settings.azure_api_base == "https://example.openai.azure.com"
    assert settings.review_model == "azure/reviewer"
