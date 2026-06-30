from app.graph.sdk import graph_name


def test_graph_name():
    assert graph_name("octo", "repo") == "graph:octo:repo:main"
    assert graph_name("octo", "repo", "pr:1") == "graph:octo:repo:pr:1"
