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


def test_status_endpoint(monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "list_graph_builds",
        lambda: [{"owner": "o", "repo": "r", "graph_scope": "main", "status": "built", "source_count": 3, "updated_at": "2026-01-01T00:00:00"}],
    )
    client = TestClient(app)

    response = client.get("/status")

    assert response.status_code == 200
    assert response.json()["builds"][0]["repo"] == "r"


def test_status_endpoint_survives_db_error(monkeypatch):
    import app.main as main

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "list_graph_builds", _boom)
    client = TestClient(app)

    response = client.get("/status")

    assert response.status_code == 200
    assert response.json() == {"builds": []}


def test_dashboard_served():
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Graph Build Status" in response.text
