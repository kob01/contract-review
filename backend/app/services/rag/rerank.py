"""Cross-encoder 重排（TEI bge-reranker-v2-m3）。

召回解决"别漏"，重排解决"谁真的相关"：RRF 只看名次，读不懂"这条是程序性管辖、
那条才是利率上限"的差别；而 bge-reranker 把 (查询, 候选条文) 拼成一对再过一次
交叉编码，分数与具体条款语义直接相关。

代价是每条候选都要一次前向计算，因此只重排召回后的少量候选，且失败时退回
RRF 顺序——重排是精度增强，不能变成审查可用性的依赖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger

logger = get_logger(__name__)


class RerankUnavailable(RuntimeError):
    """TEI 服务不可达或返回结构异常。"""


@dataclass(slots=True)
class RerankOutcome:
    """重排结果。`applied=False` 表示调用方应保持原有名次。"""

    applied: bool = False
    scores: dict[str, float] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    message: str = ""


class RerankClient:
    name = "tei_cross_encoder"

    def __init__(self, *, base_url: str, model: str, min_score: float, timeout_seconds: float = 30.0) -> None:
        import httpx

        self.model = model
        self.min_score = min_score
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds)

    async def rerank(self, query: str, candidates: list[tuple[str, str]]) -> RerankOutcome:
        """给候选打相关性分。

        `candidates` 为 (条文主键, 送去打分的文本) 且顺序即 RRF 名次；返回结果里
        低于 `min_score` 的条目进 `dropped`，由调用方剔除——这正是"召回噪声"该被
        拦住的那一步：向量与 BM25 都能靠一个共享词命中，cross-encoder 不会。
        """
        if not candidates or not query.strip():
            return RerankOutcome()

        payload = {
            "query": query,
            "texts": [text for _, text in candidates],
            "top_n": len(candidates),
            "return_text": False,
        }
        try:
            response = await self._client.post("/rerank", json=payload)
            response.raise_for_status()
            results = response.json()
        except Exception as exc:
            raise RerankUnavailable(f"调用重排服务失败：{exc}") from exc

        if not isinstance(results, list):
            raise RerankUnavailable(f"重排服务返回结构异常：{str(results)[:200]}")

        scores: dict[str, float] = {}
        dropped: list[str] = []
        for item in results:
            index = item.get("index")
            if not isinstance(index, int) or not (0 <= index < len(candidates)):
                continue
            provision_id = candidates[index][0]
            score = float(item.get("score") or 0.0)
            if score < self.min_score:
                dropped.append(provision_id)
                continue
            scores[provision_id] = score

        return RerankOutcome(
            applied=bool(scores),
            scores=scores,
            dropped=dropped,
            message=f"{len(scores)} 条通过，{len(dropped)} 条低于阈值 {self.min_score}",
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def build_reranker(settings) -> RerankClient | None:
    """按配置构造重排客户端；关闭时返回 None，检索只走 RRF 顺序。"""
    if not settings.rag_rerank_enabled:
        return None
    return RerankClient(
        base_url=settings.rag_rerank_url,
        model=settings.rag_rerank_model,
        min_score=settings.rag_rerank_min_score,
        timeout_seconds=settings.rag_rerank_timeout_seconds,
    )


#: 与 BM25 通道同理：连接池按进程复用，不按请求新建
_shared_client: RerankClient | None = None
_shared_key: str = ""


def shared_reranker(settings) -> RerankClient | None:
    """取共享重排客户端；服务地址或阈值变了会重建。"""
    global _shared_client, _shared_key

    if not settings.rag_rerank_enabled:
        return None

    key = f"{settings.rag_rerank_url}::{settings.rag_rerank_model}::{settings.rag_rerank_min_score}"
    if _shared_client is None or _shared_key != key:
        _shared_client = build_reranker(settings)
        _shared_key = key
    return _shared_client


async def aclose_shared_reranker() -> None:
    """应用关闭时释放连接。"""
    global _shared_client, _shared_key
    if _shared_client is not None:
        await _shared_client.aclose()
    _shared_client = None
    _shared_key = ""
