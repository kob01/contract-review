"""依赖注入。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings, settings
from app.db.session import get_db
from app.services.rag.embeddings import EmbeddingClient
from app.services.rag.rerank import aclose_shared_reranker, shared_reranker
from app.services.rag.retriever import ProvisionRetriever
from app.services.rag.sparse import SparseSearchIndex, aclose_shared_sparse_index, shared_sparse_index

__all__ = [
    "ProvisionRetriever",
    "Settings",
    "build_retriever",
    "close_search_clients",
    "get_db",
    "get_settings",
    "get_sparse_index",
]


def get_sparse_index() -> SparseSearchIndex | None:
    """BM25 通道；`RAG_SPARSE_ENABLED=false` 时返回 None，检索少一路召回。"""
    return shared_sparse_index(settings)


async def close_search_clients() -> None:
    """应用关闭时释放检索侧的外部连接。"""
    await aclose_shared_sparse_index()
    await aclose_shared_reranker()


def build_retriever(
    session: AsyncSession, embedder: EmbeddingClient, app_settings: Settings
) -> ProvisionRetriever:
    """统一装配检索器：向量 + BM25 + 重排走同一份配置，避免各入口参数漂移。"""
    return ProvisionRetriever(
        session,
        embedder,
        app_settings,
        sparse=shared_sparse_index(app_settings),
        reranker=shared_reranker(app_settings),
    )
