from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Settings, get_settings


class GraphSession(Protocol):
    def ask(self, question: str) -> str:
        ...


class GraphClient(Protocol):
    def process_sources(self, sources: list[object]) -> None:
        ...

    def chat_session(self) -> GraphSession:
        ...


@dataclass(frozen=True)
class TextSource:
    text: str


class SDKChatSession:
    def __init__(self, rag: Any):
        self._rag = rag

    def ask(self, question: str) -> str:
        result = self._rag.completion_sync(question)
        return str(getattr(result, "answer", result))


class SDKGraphClient:
    def __init__(self, rag: Any):
        self._rag = rag

    def process_sources(self, sources: list[object]) -> None:
        for index, source in enumerate(sources):
            text = _source_text(source)
            document_id = _document_id(text, index)
            self._rag.ingest_sync(text=text, document_id=document_id)

    def chat_session(self) -> SDKChatSession:
        return SDKChatSession(self._rag)


class HashEmbedder:
    @property
    def model_name(self) -> str:
        return "graphreview-hash-embedder-256"

    def embed_query(self, text: str, **_: object) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = list(digest) * 8
        return [(value / 127.5) - 1.0 for value in values[:256]]

    def embed_documents(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        return [self.embed_query(text, **kwargs) for text in texts]

    async def aembed_query(self, text: str, **kwargs: object) -> list[float]:
        return self.embed_query(text, **kwargs)

    async def aembed_documents(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        return self.embed_documents(texts, **kwargs)


def graph_name(owner: str, repo: str, suffix: str = "main") -> str:
    return f"graph:{owner}:{repo}:{suffix}"


def create_source(text: str) -> object:
    return TextSource(text)


def get_kg(owner: str, repo: str, suffix: str = "main", settings: Settings | None = None) -> GraphClient:
    settings = settings or get_settings()
    try:
        from graphrag_sdk import ConnectionConfig, GraphRAG, LiteLLM
    except ImportError as exc:
        raise RuntimeError("graphrag-sdk[litellm] is required for knowledge graph operations") from exc

    llm = LiteLLM(
        model=settings.review_model,
        api_key=settings.azure_api_key or None,
        api_base=settings.azure_api_base or None,
        api_version=settings.azure_api_version or None,
        max_tokens=4096,
    )
    connection = ConnectionConfig(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        graph_name=graph_name(owner, repo, suffix),
    )
    rag = GraphRAG(
        connection=connection,
        llm=llm,
        embedder=HashEmbedder(),
        schema=_build_code_schema(),
        embedding_dimension=256,
    )
    return SDKGraphClient(rag)


def _build_code_schema() -> object:
    from graphrag_sdk import EntityType, GraphSchema, RelationType

    entities = [
        EntityType(label="File", description="A source code file with a path and language."),
        EntityType(label="Function", description="A function, method, or callable symbol."),
        EntityType(label="Class", description="A class, struct, interface, or type."),
        EntityType(label="Module", description="An imported or exported code module."),
        EntityType(label="Test", description="A test file, test case, or test function."),
    ]
    relations = [
        RelationType(label="CALLS", patterns=[("Function", "Function"), ("Test", "Function")]),
        RelationType(label="DEFINED_IN", patterns=[("Function", "File"), ("Class", "File"), ("Test", "File")]),
        RelationType(label="IMPORTS", patterns=[("File", "File"), ("File", "Module")]),
        RelationType(label="HAS_METHOD", patterns=[("Class", "Function")]),
        RelationType(label="TESTS", patterns=[("Test", "Function"), ("Test", "Class"), ("Test", "File")]),
        RelationType(label="DEPENDS_ON", patterns=[]),
    ]
    return GraphSchema(entities=entities, relations=relations)


def _source_text(source: object) -> str:
    for attribute in ("text", "content"):
        value = getattr(source, attribute, None)
        if isinstance(value, str):
            return value
    if isinstance(source, str):
        return source
    raise TypeError(f"Unsupported graph source type: {type(source).__name__}")


def _document_id(text: str, index: int) -> str:
    first_line = text.splitlines()[0] if text else f"source:{index}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{first_line}:{digest}"
