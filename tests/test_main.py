from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_health():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_webhook_rejects_bad_signature(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "secret")
    client = TestClient(app)

    response = client.post(
        "/webhook",
        content=b"{}",
        headers={"x-github-event": "push", "x-hub-signature-256": "sha256=bad"},
    )

    assert response.status_code == 401
