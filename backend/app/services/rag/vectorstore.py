"""稠密向量库：Qdrant 加速层。

只负责"相似召回"：集合里存 {主键, 向量, is_active/is_sample/doc_type}，检索返回 `(主键, cosine 分值)`。
权威原文与其余字段一律按主键回 `legal_provisions` 取（与 BM25 通道同构），这样引用校验仍走
数据库主键，向量库不会变成另一张影子表。

降级口径与 ES 一致：Qdrant 不可达时抛 `VectorStoreUnavailable`，调用方（检索器）据此只关掉
稠密这一路，关键词 / BM25 / 直连照常，不阻塞审查。

维度处理：qwen3-embedding:8b 原生 4096，Qdrant 没有 pgvector 的 2000 维索引上限，直接存原生维度。
`ensure_collection` 发现既有集合维度与配置不符（如历史遗留的 1024）时 drop 重建——旧向量属于另一个
模型/维度空间，留着只会让相似度静默失真却不报错。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class VectorStoreUnavailable(RuntimeError):
    """Qdrant 不可用或未就绪；调用方应降级为其余召回通道。"""


@dataclass(slots=True)
class DenseDoc:
    """一条待写入 Qdrant 的向量。`record_id` 直接用主键，命中后能回连主表。"""

    record_id: str
    vector: list[float]
    is_active: bool
    is_sample: bool
    doc_type: str = ""

    def to_payload(self) -> dict:
        return {"is_active": self.is_active, "is_sample": self.is_sample, "doc_type": self.doc_type}


def describe_vector_store(_: Settings) -> str:
    """不建连接地描述稠密向量后端，供 /health 与统计接口复用。"""
    return QdrantVectorStore.name


class QdrantVectorStore:
    name = "qdrant"

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        timeout_seconds: int,
        dimensions: int,
        provisions_collection: str,
        cases_collection: str,
    ) -> None:
        from qdrant_client import AsyncQdrantClient

        self.dimensions = dimensions
        self.provisions_collection = provisions_collection
        self.cases_collection = cases_collection
        self._url = url
        self._client = AsyncQdrantClient(url=url, api_key=api_key or None, timeout=timeout_seconds)
        #: 已确认存在且维度匹配的集合，避免每次操作都探一遍
        self._ready: set[str] = set()

    async def ensure_collection(self, collection: str) -> None:
        """不存在就按当前维度建；存在但维度不符（换模型/换维）则 drop 重建。"""
        from qdrant_client.models import Distance, VectorParams

        if collection in self._ready:
            return
        try:
            exists = await self._client.collection_exists(collection)
            if exists:
                info = await self._client.get_collection(collection)
                size = self._existing_size(info)
                if size is not None and size != self.dimensions:
                    logger.warning(
                        "Qdrant 集合 %s 维度为 %s，与 EMBEDDING_DIMENSIONS=%s 不符，重建并等待重算",
                        collection,
                        size,
                        self.dimensions,
                    )
                    await self._client.delete_collection(collection)
                    exists = False
            if not exists:
                await self._client.create_collection(
                    collection_name=collection,
                    vectors_config=VectorParams(size=self.dimensions, distance=Distance.COSINE),
                )
                logger.info("已创建 Qdrant 集合 %s（%s 维，cosine）", collection, self.dimensions)
            self._ready.add(collection)
        except VectorStoreUnavailable:
            raise
        except Exception as exc:
            raise VectorStoreUnavailable(f"初始化 Qdrant 集合 {collection} 失败：{exc}") from exc

    @staticmethod
    def _existing_size(info) -> int | None:
        """从 CollectionInfo 里取向量列维度；结构随客户端版本有差异，取不到就返回 None 跳过比对。"""
        try:
            vectors = info.config.params.vectors
            # 单向量时 vectors 即 VectorParams；具名向量时为 dict，取默认段
            params = next(iter(vectors.values())) if isinstance(vectors, dict) else vectors
            return int(params.size)
        except Exception:
            return None

    async def upsert(self, collection: str, docs: list[DenseDoc]) -> int:
        if not docs:
            return 0
        from qdrant_client.models import PointStruct

        await self._require_ready(collection)
        points = [
            PointStruct(id=doc.record_id, vector=doc.vector, payload=doc.to_payload())
            for doc in docs
        ]
        try:
            await self._client.upsert(collection_name=collection, points=points, wait=True)
        except Exception as exc:
            raise VectorStoreUnavailable(f"写入 Qdrant 集合 {collection} 失败：{exc}") from exc
        return len(docs)

    async def delete(self, collection: str, record_id: str) -> None:
        if not record_id:
            return
        from qdrant_client.models import PointIdsList

        try:
            await self._client.delete(
                collection_name=collection,
                points_selector=PointIdsList(points=[record_id]),
                wait=True,
            )
        except Exception as exc:  # 删除失败不应阻塞主表删除主流程
            logger.warning("从 Qdrant 集合 %s 删除 %s 失败：%s", collection, record_id, exc)

    async def search(
        self, collection: str, vector: list[float], *, limit: int, only_active: bool = True
    ) -> list[tuple[str, float]]:
        """返回 (主键, cosine 相似度)，按分值降序。"""
        if not vector:
            return []
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        await self._require_ready(collection)
        query_filter = (
            Filter(must=[FieldCondition(key="is_active", match=MatchValue(value=True))])
            if only_active
            else None
        )
        try:
            hits = await self._client.search(
                collection_name=collection,
                query_vector=vector,
                limit=limit,
                query_filter=query_filter,
                with_payload=False,
            )
        except Exception as exc:
            raise VectorStoreUnavailable(f"Qdrant 检索失败：{exc}") from exc
        return [(str(hit.id), float(hit.score)) for hit in hits]

    async def count(self, collection: str) -> int:
        await self._require_ready(collection)
        try:
            result = await self._client.count(collection_name=collection, exact=True)
        except Exception as exc:
            raise VectorStoreUnavailable(f"统计 Qdrant 集合失败：{exc}") from exc
        return int(result.count)

    async def aclose(self) -> None:
        await self._client.close()

    async def _require_ready(self, collection: str) -> None:
        if collection in self._ready:
            return
        try:
            # 轻量连通性探测：拉一次集合列表，不新建连接、不读数据
            await self._client.get_collections()
        except VectorStoreUnavailable:
            raise
        except Exception as exc:
            raise VectorStoreUnavailable(f"Qdrant 不可达（{self._url}）：{exc}") from exc
        await self.ensure_collection(collection)


def build_vectorstore(settings: Settings) -> QdrantVectorStore:
    return QdrantVectorStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        timeout_seconds=settings.qdrant_timeout_seconds,
        dimensions=settings.embedding_dimensions,
        provisions_collection=settings.qdrant_collection,
        cases_collection=settings.qdrant_cases_collection,
    )


#: 进程级单例：与向量/模型客户端同一口径，重复构造会把连接池拆散
_shared_store: QdrantVectorStore | None = None
_shared_key: str = ""


def shared_vectorstore(settings: Settings) -> QdrantVectorStore:
    """取共享稠密向量库；地址 / 集合名 / 维度变了才重建。"""
    global _shared_store, _shared_key

    key = (
        f"{settings.qdrant_url}::{settings.qdrant_collection}"
        f"::{settings.qdrant_cases_collection}::{settings.embedding_dimensions}"
    )
    if _shared_store is None or _shared_key != key:
        _shared_store = build_vectorstore(settings)
        _shared_key = key
    return _shared_store


async def aclose_shared_vectorstore() -> None:
    """应用关闭时释放 Qdrant 连接。"""
    global _shared_store, _shared_key
    if _shared_store is not None:
        await _shared_store.aclose()
    _shared_store = None
    _shared_key = ""
