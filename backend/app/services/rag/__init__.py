"""RAG 检索层。"""

from app.services.rag.embeddings import (
    EmbeddingClient,
    EmbeddingNotConfigured,
    HashingEmbeddings,
    OpenAICompatEmbeddings,
    build_embedding_client,
)
from app.services.rag.ingest import (
    PLACEHOLDER_PREFIX,
    ProvisionInput,
    ensure_registry,
    rebuild_missing_embeddings,
    upsert_enforcement_case,
    upsert_provision,
)
from app.services.rag.retriever import ProvisionRetriever, RetrievedProvision

__all__ = [
    "PLACEHOLDER_PREFIX",
    "EmbeddingClient",
    "EmbeddingNotConfigured",
    "HashingEmbeddings",
    "OpenAICompatEmbeddings",
    "ProvisionInput",
    "ProvisionRetriever",
    "RetrievedProvision",
    "build_embedding_client",
    "ensure_registry",
    "rebuild_missing_embeddings",
    "upsert_enforcement_case",
    "upsert_provision",
]
