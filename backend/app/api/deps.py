"""依赖注入与进程级客户端装配。

这里的函数都只"取单例"，不"建新的"：模型、向量、BM25、重排四类外部客户端
按进程复用，退出时由 `close_app_clients` 统一释放。

为什么不用 `app.state` 存这些对象：worker 进程不经过 FastAPI，
把单例挂在应用对象上会让"API 与 worker 共用同一套装配口径"变成两句代码。
模块级缓存 + 配置指纹重建，两边都能用，也是 /health 不新建连接的前提。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings, settings
from app.db.session import get_db
from app.services.llm.factory import (
    aclose_shared_providers,
    shared_judge_slot,
    shared_provider,
)
from app.services.rag.embeddings import EmbeddingClient, aclose_shared_embedder, shared_embedder
from app.services.rag.rerank import aclose_shared_reranker, shared_reranker
from app.services.rag.retriever import ProvisionRetriever
from app.services.rag.sparse import (
    SparseSearchIndex,
    aclose_shared_sparse_index,
    shared_sparse_index,
)
from app.services.rag.vectorstore import (
    QdrantVectorStore,
    aclose_shared_vectorstore,
    shared_vectorstore,
)

__all__ = [
    "ProvisionRetriever",
    "QdrantVectorStore",
    "Settings",
    "build_retriever",
    "close_app_clients",
    "close_search_clients",
    "get_db",
    "get_settings",
    "get_sparse_index",
    "shared_embedder",
    "shared_judge_slot",
    "shared_provider",
    "shared_vectorstore",
]


def get_sparse_index() -> SparseSearchIndex | None:
    """BM25 通道；`RAG_SPARSE_ENABLED=false` 时返回 None，检索少一路召回。"""
    return shared_sparse_index(settings)


async def close_search_clients() -> None:
    """应用关闭时释放检索侧的外部连接。"""
    await aclose_shared_sparse_index()
    await aclose_shared_vectorstore()
    await aclose_shared_reranker()
    await aclose_shared_embedder()


async def close_app_clients() -> None:
    """释放模型侧与检索侧的全部外部连接。"""
    await aclose_shared_providers()
    await close_search_clients()


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
        vector_store=shared_vectorstore(app_settings),
    )
