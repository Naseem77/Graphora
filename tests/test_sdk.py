from app.config import Settings
from app.graph.sdk import HashEmbedder, create_source, get_kg, graph_name


def test_graph_name_and_source():
    assert graph_name("octo", "repo") == "graph:octo:repo:main"
    assert graph_name("octo", "repo", "pr:1") == "graph:octo:repo:pr:1"
    assert create_source("hello").text == "hello"


def test_hash_embedder_dimension_is_stable():
    embedder = HashEmbedder()

    assert len(embedder.embed_query("hello")) == 256
    assert embedder.embed_query("hello") == embedder.embed_query("hello")


def test_get_kg_instantiates_sdk_client(tmp_path):
    settings = Settings(
        github_app_id="1",
        github_private_key_path=tmp_path / "key.pem",
        github_webhook_secret="secret",
        azure_api_key="test",
        azure_api_base="https://example.openai.azure.com",
        azure_api_version="2024-02-15-preview",
        review_model="azure/reviewer",
        falkordb_host="localhost",
    )

    kg = get_kg("octo", "repo", settings=settings)

    assert kg.__class__.__name__ == "SDKGraphClient"
