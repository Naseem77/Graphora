from app import state
from app.config import Settings


class FakeResult:
    def __init__(self, rows=None):
        self.result_set = rows or []


class FakeGraph:
    def __init__(self):
        self.queries = []
        self.deliveries = set()
        self.reviews = set()

    def query(self, query, params=None):
        params = params or {}
        self.queries.append((query, params))
        if "MATCH (delivery:WebhookDelivery" in query:
            return FakeResult([[params["id"]]] if params["id"] in self.deliveries else [])
        if "MERGE (delivery:WebhookDelivery" in query:
            self.deliveries.add(params["id"])
        if "MATCH (review:PRReview" in query:
            return FakeResult([[params["id"]]] if params["id"] in self.reviews else [])
        if "MERGE (review:PRReview" in query:
            self.reviews.add(params["id"])
        return FakeResult()


def _settings():
    return Settings(
        github_app_id="1",
        github_private_key_path="key.pem",
        github_webhook_secret="secret",
        azure_api_key="key",
        azure_api_base="https://example.openai.azure.com",
        azure_api_version="2024-02-15-preview",
        review_model="azure/reviewer",
    )


def test_review_idempotency_state(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(state, "_select_state_graph", lambda settings: fake_graph)
    settings = _settings()

    assert not state.review_already_processed("o", "r", 1, "sha", settings)
    state.mark_review_posted("o", "r", 1, "sha", settings)
    assert state.review_already_processed("o", "r", 1, "sha", settings)


def test_delivery_idempotency_state(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(state, "_select_state_graph", lambda settings: fake_graph)
    settings = _settings()

    assert state.mark_delivery("push", "delivery-1", settings)
    assert not state.mark_delivery("push", "delivery-1", settings)


def test_graph_build_state_uses_falkordb_graph(monkeypatch):
    fake_graph = FakeGraph()
    monkeypatch.setattr(state, "_select_state_graph", lambda settings: fake_graph)

    state.mark_graph_build("o", "r", "main", "built", 3, _settings())

    assert any("MERGE (build:GraphBuild" in query for query, _ in fake_graph.queries)
