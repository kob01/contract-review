"""Qdrant 向量库客户端：命中回传、维度不符重建、不可达降级。

用假 client 注入，不发真实网络：这些行为决定稠密通道能否正确召回与优雅降级，
尤其是"换模型/换维必须重建集合"这条静默正确性防线。
"""

from __future__ import annotations

import pytest
from app.services.rag.vectorstore import (
    DenseDoc,
    QdrantVectorStore,
    VectorStoreUnavailable,
)


class _VectorParams:
    def __init__(self, size: int) -> None:
        self.size = size


class _Params:
    def __init__(self, vectors: object) -> None:
        self.vectors = vectors


class _Config:
    def __init__(self, params: _Params) -> None:
        self.params = params


class _CollectionInfo:
    def __init__(self, vectors: object) -> None:
        self.config = _Config(_Params(vectors))


class _Scored:
    def __init__(self, id_: str, score: float) -> None:
        self.id = id_
        self.score = score


class _CountResult:
    count = 3


class FakeQdrant:
    """覆盖被测方法用到的最小接口；fail=True 模拟服务不可达。"""

    def __init__(self, existing_size: int = 4096) -> None:
        self.existing_size = existing_size
        self.fail = False
        self.created: list[tuple[str, int]] = []
        self.deleted: list[str] = []
        self.upsert_calls: list[tuple[str, int]] = []
        self.last_filter: object = None

    async def get_collections(self) -> object:
        if self.fail:
            raise RuntimeError("connection refused")
        return object()

    async def collection_exists(self, name: str) -> bool:
        return True

    async def get_collection(self, name: str) -> _CollectionInfo:
        return _CollectionInfo(_VectorParams(self.existing_size))

    async def create_collection(self, collection_name: str, vectors_config: object) -> None:
        self.created.append((collection_name, vectors_config.size))

    async def delete_collection(self, collection_name: str) -> None:
        self.deleted.append(collection_name)

    async def upsert(self, collection_name: str, points: list, wait: bool = True) -> None:
        self.upsert_calls.append((collection_name, len(points)))

    async def search(
        self, collection_name: str, query_vector: list, limit: int, query_filter=None, with_payload=False
    ) -> list[_Scored]:
        self.last_filter = query_filter
        return [_Scored("a", 0.9), _Scored("b", 0.4)]

    async def count(self, collection_name: str, exact: bool = True) -> _CountResult:
        return _CountResult()

    async def delete(self, collection_name: str, points_selector, wait: bool = True) -> None:
        return None

    async def close(self) -> None:
        return None


def make_store(client: FakeQdrant, dimensions: int = 4096) -> QdrantVectorStore:
    store = QdrantVectorStore(
        url="http://fake:6333",
        api_key="",
        timeout_seconds=5,
        dimensions=dimensions,
        provisions_collection="prov",
        cases_collection="cases",
    )
    store._client = client  # 注入假客户端，绕开真实连接
    return store


async def test_search_returns_id_and_score():
    store = make_store(FakeQdrant())
    hits = await store.search("prov", [0.1] * 4096, limit=5, only_active=True)
    assert hits == [("a", 0.9), ("b", 0.4)]


async def test_ensure_collection_recreates_when_dim_mismatch():
    """既有集合 1024、配置 4096：必须 drop 重建，避免新旧维度混空间。"""
    fake = FakeQdrant(existing_size=1024)
    store = make_store(fake, dimensions=4096)
    await store.ensure_collection("prov")
    assert "prov" in fake.deleted
    assert fake.created == [("prov", 4096)]


async def test_ensure_collection_keeps_when_dim_matches():
    fake = FakeQdrant(existing_size=4096)
    store = make_store(fake, dimensions=4096)
    await store.ensure_collection("prov")
    assert fake.deleted == []
    assert fake.created == []  # 已存在且维度一致，不新建


async def test_search_raises_unavailable_when_down():
    fake = FakeQdrant()
    fake.fail = True
    store = make_store(fake)
    with pytest.raises(VectorStoreUnavailable):
        await store.search("prov", [0.1] * 4096, limit=3)


async def test_upsert_and_count():
    fake = FakeQdrant()
    store = make_store(fake)
    doc = DenseDoc(record_id="a", vector=[0.1] * 4096, is_active=True, is_sample=False, doc_type="law")
    assert await store.upsert("prov", [doc]) == 1
    assert fake.upsert_calls == [("prov", 1)]
    assert await store.count("prov") == 3
