from app.graph import pr_context


def test_render_impact_formats_callers_and_tests():
    impacts = [
        {
            "name": "login",
            "definitions": [{"kind": "Function", "path": "app/auth.py", "line": 10, "signature": "def login()"}],
            "callers": [{"name": "handler", "path": "app/api.py"}],
            "callees": [{"name": "hash_pw", "path": "app/crypto.py"}],
            "tests": [{"name": "test_login", "path": "tests/test_auth.py"}],
        }
    ]

    text = pr_context.render_impact(impacts, ["login"])

    assert "### `login`" in text
    assert "defined as Function at `app/auth.py:10`" in text
    assert "callers (1): `handler` (app/api.py)" in text
    assert "calls (1): `hash_pw` (app/crypto.py)" in text
    assert "tests (1): `test_login` (tests/test_auth.py)" in text


def test_render_impact_handles_no_results():
    assert "no callers" in pr_context.render_impact([], ["login"])
    assert "no changed symbols" in pr_context.render_impact([], [])


def test_build_pr_context_combines_diff_and_impact(monkeypatch):
    monkeypatch.setattr(pr_context, "extract_symbols_from_diff", lambda diff: ["login"])

    class FakeDiff:
        def to_prompt_context(self):
            return "STRUCTURED DIFF"

    monkeypatch.setattr(pr_context, "diff_main_vs_pr", lambda owner, repo, pr, settings: FakeDiff())
    monkeypatch.setattr(
        pr_context,
        "symbol_impact",
        lambda owner, repo, symbols, settings, suffix="main": [
            {"name": "login", "definitions": [], "callers": [], "callees": [], "tests": []}
        ],
    )

    context = pr_context.build_pr_context("o", "r", "diff", pr_number=5, settings=object())

    assert "STRUCTURED DIFF" in context
    assert "Codebase impact" in context


def test_build_pr_context_without_pr_number_skips_diff(monkeypatch):
    monkeypatch.setattr(pr_context, "extract_symbols_from_diff", lambda diff: [])
    monkeypatch.setattr(pr_context, "symbol_impact", lambda owner, repo, symbols, settings, suffix="main": [])

    context = pr_context.build_pr_context("o", "r", "diff", pr_number=None, settings=object())

    assert "no changed symbols" in context
