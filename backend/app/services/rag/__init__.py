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
    index_vectors,
    sync_vector_store,
    upsert_enforcement_case,
    upsert_provision,
)
from app.services.rag.retriever import ProvisionRetriever, RetrievedProvision
from app.services.rag.vectorstore import (
    QdrantVectorStore,
    VectorStoreUnavailable,
    shared_vectorstore,
)

__all__ = [
    "PLACEHOLDER_PREFIX",
    "EmbeddingClient",
    "EmbeddingNotConfigured",
    "HashingEmbeddings",
    "OpenAICompatEmbeddings",
    "ProvisionInput",
    "ProvisionRetriever",
    "QdrantVectorStore",
    "RetrievedProvision",
    "VectorStoreUnavailable",
    "build_embedding_client",
    "ensure_registry",
    "index_vectors",
    "shared_vectorstore",
    "sync_vector_store",
    "upsert_enforcement_case",
    "upsert_provision",
]
