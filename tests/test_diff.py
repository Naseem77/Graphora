from app.graph.diff import GraphDiff, _keyed_rows, _symbol_path


class Result:
    result_set = [["a", "1"], ["b", "2"]]


class Graph:
    def query(self, query):
        return Result()


def test_keyed_rows_extracts_result_set():
    assert _keyed_rows(Graph(), "MATCH") == {"a": "1", "b": "2"}


def test_graph_diff_prompt_context():
    context = GraphDiff(["a.py"], ["b.py"], ["x"], ["y"], ["z"]).to_prompt_context()

    assert "Added files: a.py" in context
    assert "Changed symbols: y" in context


def test_symbol_path_from_stable_key():
    assert _symbol_path("app/a.py:Function:run") == "app/a.py"
