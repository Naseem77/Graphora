from app.ci import graph_context


class Result:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self):
        self.queries = []

    def query(self, query, params=None):
        self.queries.append((query, params or {}))
        if "shortestPath" in query:
            return Result([["tests/test_billing.py", "app/refunds.py", ["tests/test_billing.py", "app/billing/client.py", "app/refunds.py"]]])
        return Result([["app/refunds.py:Function:refund"]])


def test_build_ci_graph_evidence_uses_pr_graph(monkeypatch, tmp_path):
    fake_graph = FakeGraph()
    selected = {}

    def fake_select(owner, repo, settings, suffix="main"):
        selected["suffix"] = suffix
        return fake_graph

    monkeypatch.setattr(graph_context, "_select_graph", fake_select)

    evidence = graph_context.build_ci_graph_evidence(
        "octo",
        "repo",
        7,
        ["app/refunds.py"],
        ["tests/test_billing.py::test_refund_status"],
        [],
    )

    assert selected["suffix"] == "pr:7"
    assert evidence.failed_paths == ["tests/test_billing.py"]
    assert evidence.dependency_paths == ["tests/test_billing.py -> app/billing/client.py -> app/refunds.py"]
    assert evidence.related_symbols == ["app/refunds.py:Function:refund"]
