"""稀疏检索通道：Elasticsearch BM25 + jieba 预分词。

为什么库里已经有 ILIKE 关键词召回还要接 ES：

1. 中文分词。ILIKE 只能整串匹配，"综合融资成本"命中不了"融资综合成本"，
   而法条与合同表述的词序经常不一致；
2. 排序信号。BM25 带 tf-idf 与字段长度归一，能给出一条**可比较的名次**，
   这是与向量结果做 RRF 融合的前提；ILIKE 只会返回"匹配/不匹配"。

分词一律在应用侧用 jieba 完成，把切好的词用空格拼进 `whitespace` 分析器字段：
目标 ES 集群通常装不上 analysis-jieba 插件，而写入与查询两侧共用同一套词典
反而更可控（不会出现索引期分词、检索期不分的错配）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 停用词只为降噪，不影响召回完整性：BM25 本身会压低高频词
_STOPWORDS = frozenset(
    {
        "的",
        "了",
        "并",
        "及",
        "或",
        "与",
        "和",
        "应当",
        "可以",
        "不得",
        "本条",
        "按照",
        "根据",
        "以及",
        "其中",
        "进行",
        "有关",
        "规定",
        "要求",
        "本",
        "该",
    }
)

_INDEX_BODY: dict = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    "mappings": {
        "properties": {
            # 应用侧已用 jieba 切好词，这里只按空白再拆一次
            "tokens": {"type": "text", "analyzer": "whitespace", "search_analyzer": "whitespace"},
            "law_name": {"type": "keyword"},
            "article_no": {"type": "keyword"},
            "doc_type": {"type": "keyword"},
            "content": {"type": "text", "index": False},
            "is_sample": {"type": "boolean"},
            "is_active": {"type": "boolean"},
        }
    },
}


class SparseSearchUnavailable(RuntimeError):
    """ES 不可用或未就绪，调用方应降级为其余召回通道。"""


@dataclass(slots=True)
class SparseDoc:
    """一条待索引的条文。`_id` 直接用条文主键，融合时能回连主表。"""

    provision_id: str
    law_name: str
    article_no: str
    doc_type: str
    content: str
    is_sample: bool
    is_active: bool

    @property
    def indexed_text(self) -> str:
        return f"{self.law_name} {self.article_no} {self.content}"


def tokenize(text: str) -> list[str]:
    """jieba 搜索型切分（同时产出复合词与子词），再过滤停用词与单字标点。"""
    import jieba

    tokens: list[str] = []
    for raw in jieba.lcut_for_search(text or ""):
        token = raw.strip()
        if not token or token in _STOPWORDS:
            continue
        # 单个汉字与标点基本是噪声；ASCII 片段（如 "24%"、"LPR"）整段保留
        if len(token) == 1 and not token.isascii():
            continue
        tokens.append(token.lower() if token.isascii() else token)
    return tokens


async def atokenize(text: str) -> list[str]:
    """jieba 首次加载词典是秒级 CPU 活，丢到线程里避免卡住事件循环。"""
    return await asyncio.to_thread(tokenize, text)


class SparseSearchIndex:
    name = "elasticsearch_bm25"

    def __init__(
        self,
        *,
        url: str,
        index: str,
        min_should_match: str = "30%",
        timeout_seconds: float = 10.0,
    ) -> None:
        from elasticsearch import AsyncElasticsearch

        self.index = index
        self._min_should_match = min_should_match
        self._client = AsyncElasticsearch(hosts=[url], request_timeout=timeout_seconds)
        self._ready = False

    async def ensure_index(self) -> None:
        """不存在就按映射建索引；存在则直接复用。"""
        try:
            if await self._client.indices.exists(index=self.index):
                self._ready = True
                return
            await self._client.indices.create(index=self.index, **_INDEX_BODY)
            self._ready = True
            logger.info("已创建 BM25 索引 %s", self.index)
        except Exception as exc:
            raise SparseSearchUnavailable(f"初始化 BM25 索引 {self.index} 失败：{exc}") from exc

    async def upsert(self, docs: list[SparseDoc]) -> int:
        """批量写入。分词在 to_thread 里做，索引写满后立即 refresh 便于自检。"""
        if not docs:
            return 0
        await self._require_ready()

        operations: list[dict] = []
        for doc in docs:
            tokens = await atokenize(doc.indexed_text)
            operations.append({"index": {"_index": self.index, "_id": doc.provision_id}})
            operations.append(
                {
                    "tokens": " ".join(tokens),
                    "law_name": doc.law_name,
                    "article_no": doc.article_no,
                    "doc_type": doc.doc_type,
                    "content": doc.content,
                    "is_sample": doc.is_sample,
                    "is_active": doc.is_active,
                }
            )

        try:
            response = await self._client.bulk(operations=operations, refresh=True)
        except Exception as exc:
            raise SparseSearchUnavailable(f"写入 BM25 索引失败：{exc}") from exc

        if response.get("errors"):
            failed = [
                f"{item.get('index', {}).get('_id')}: {item.get('index', {}).get('error')}"
                for item in response.get("items", [])
                if item.get("index", {}).get("error")
            ]
            logger.warning("BM25 索引部分写入失败：%s", "; ".join(failed[:3]))
        return len(docs)

    async def delete(self, provision_id: str) -> None:
        if not provision_id:
            return
        from elasticsearch import NotFoundError

        try:
            await self._client.delete(index=self.index, id=provision_id, refresh=True)
        except NotFoundError:
            return
        except Exception as exc:  # 删除失败不应阻塞条文删除主流程
            logger.warning("从 BM25 索引删除 %s 失败：%s", provision_id, exc)

    async def search(
        self, query: str, *, size: int = 30, only_active: bool = True
    ) -> list[tuple[str, float]]:
        """返回 (条文主键, BM25 分值)，按分值降序。"""
        await self._require_ready()
        tokens = await atokenize(query)
        if not tokens:
            return []

        query = {
            "bool": {
                "must": [
                    {
                        "match": {
                            "tokens": {
                                "query": " ".join(tokens),
                                "minimum_should_match": self._min_should_match,
                            }
                        }
                    }
                ],
                "filter": [{"term": {"is_active": True}}] if only_active else [],
            }
        }

        try:
            response = await self._client.search(
                index=self.index, size=size, source=False, query=query
            )
        except Exception as exc:
            raise SparseSearchUnavailable(f"BM25 检索失败：{exc}") from exc

        hits = response.get("hits", {}).get("hits", [])
        return [(str(hit.get("_id")), float(hit.get("_score") or 0.0)) for hit in hits]

    async def count(self) -> int:
        try:
            response = await self._client.count(index=self.index)
        except Exception as exc:
            raise SparseSearchUnavailable(f"统计 BM25 索引失败：{exc}") from exc
        return int(response.get("count", 0))

    async def aclose(self) -> None:
        await self._client.close()

    async def _require_ready(self) -> None:
        if self._ready:
            return
        try:
            if not await self._client.ping():
                raise SparseSearchUnavailable(f"Elasticsearch 不可达（索引 {self.index}）")
        except SparseSearchUnavailable:
            raise
        except Exception as exc:
            raise SparseSearchUnavailable(f"Elasticsearch 连通性检查失败：{exc}") from exc
        await self.ensure_index()


def build_sparse_index(settings) -> SparseSearchIndex | None:
    """按配置构造稀疏通道；未启用时返回 None，调用方据此跳过该路召回。"""
    if not settings.rag_sparse_enabled:
        return None
    return SparseSearchIndex(
        url=settings.es_url,
        index=settings.es_index,
        min_should_match=settings.rag_sparse_min_should_match,
    )


#: 进接口层与后台任务共用的进进程级客户端：ES 客户端建连接是惰性的，
#: 但重复构造会把连接池拆散，因而不是按请求新建
_shared_index: SparseSearchIndex | None = None
_shared_key: str = ""


def shared_sparse_index(settings) -> SparseSearchIndex | None:
    """取共享稀疏通道；ES 地址或索引名变了会重建（便于改配置与测试）。"""
    global _shared_index, _shared_key

    if not settings.rag_sparse_enabled:
        return None

    key = f"{settings.es_url}::{settings.es_index}::{settings.rag_sparse_min_should_match}"
    if _shared_index is None or _shared_key != key:
        _shared_index = build_sparse_index(settings)
        _shared_key = key
    return _shared_index


async def aclose_shared_sparse_index() -> None:
    """应用关闭时释放连接。"""
    global _shared_index, _shared_key
    if _shared_index is not None:
        await _shared_index.aclose()
    _shared_index = None
    _shared_key = ""
