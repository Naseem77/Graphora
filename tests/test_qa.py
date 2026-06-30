from types import SimpleNamespace

from app.review import qa


def _settings(**kwargs):
    base = {"azure_api_key": "", "azure_api_base": "", "azure_api_version": "2024-02-01", "review_model": ""}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_graph_context_includes_overview_and_impact(monkeypatch):
    monkeypatch.setattr(
        qa,
        "code_graph_stats",
        lambda owner, repo, settings: {
            "node_total": 10,
            "node_counts": {"File": 4, "Function": 6},
            "relationship_count": 12,
            "files": ["a.py"],
        },
    )
    monkeypatch.setattr(
        qa,
        "symbol_impact",
        lambda owner, repo, candidates, settings, suffix="main": [
            {"name": "login", "definitions": [], "callers": [], "callees": [], "tests": []}
        ],
    )

    context = qa._graph_context("o", "r", "what does login do?", _settings())

    assert "Repository graph overview: 10 nodes" in context
    assert "Codebase impact" in context


def test_answer_repo_question_returns_context_when_model_unconfigured(monkeypatch):
    monkeypatch.setattr(qa, "_graph_context", lambda owner, repo, question, settings: "CONTEXT")

    answer = qa.answer_repo_question("o", "r", "anything", _settings())

    assert "review model is not configured" in answer
    assert "CONTEXT" in answer


def test_answer_repo_question_uses_llm_when_configured(monkeypatch):
    monkeypatch.setattr(qa, "_graph_context", lambda owner, repo, question, settings: "CONTEXT")

    def fake_completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="LLM answer"))])

    monkeypatch.setattr(qa.litellm, "completion", fake_completion)

    settings = _settings(azure_api_key="k", azure_api_base="https://x", review_model="azure/gpt-4.1")
    answer = qa.answer_repo_question("o", "r", "anything", settings)

    assert answer == "LLM answer"
