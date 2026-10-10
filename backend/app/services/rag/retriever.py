"""混合检索：法条精确匹配 + 关键词 + 向量相似度。

为什么不只用向量检索：法律判断里"哪一部法的第几条"是离散标识，
embedding 会把"第二十六条"和"第二十七条"视为近义，直接走向量会产生
张冠李戴的引用。因此本检索器把三种召回合并：

1. 条文直连：规则库 `basis_refs` 指定的（法规名, 条号）必然命中，权重最高；
2. 关键词：按法规名与内容做 ILIKE 匹配，覆盖"民法典第六百七十条"这类提法；
3. 向量：pgvector 余弦相似度，负责同义表述与罚单类比。

向量分支使用 LlamaIndex 的 `BaseRetriever`/`TextNode`/`NodeWithScore` 契约，
检索执行仍是原生 SQL —— 引用必须能直接回连 `legal_provisions` 主键，
把向量库存成另一张影子表会让溯源校验失去意义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.knowledge import LegalProvision
from app.services.rag.embeddings import EmbeddingClient

logger = get_logger(__name__)

#: 参与关键词召回的高信息量词，避免"的/了/应当"这类噪声主导
_KEYWORD_STOPWORDS = {"的", "了", "并", "及", "或", "与", "应当", "可以", "不得", "本条"}

_ARTICLE_RE = re.compile(r"第[0-9一二三四五六七八九十百零〇]+条")


@dataclass(slots=True)
class RetrievedProvision:
    """一条召回结果，同时是"引用"落库前的校验载体。"""

    provision_id: str
    law_name: str
    article_no: str
    content: str
    doc_type: str
    is_sample: bool
    score: float
    match_kinds: list[str] = field(default_factory=list)
    effective_on: str | None = None
    repealed_on: str | None = None
    source_url: str | None = None

    def to_citation(self, verified: bool = True) -> dict:
        return {
            "provisionId": self.provision_id,
            "lawName": self.law_name,
            "articleNo": self.article_no,
            "quote": self.content,
            "docType": self.doc_type,
            "isSample": self.is_sample,
            "verified": verified,
            "effectiveOn": self.effective_on,
            "repealedOn": self.repealed_on,
            "sourceUrl": self.source_url,
        }

    def to_snapshot(self) -> dict:
        """归档到 `ContractReview.retrieved_provisions`，保留命中方式与分值。"""
        payload = self.to_citation(verified=not self.is_sample)
        payload["score"] = round(self.score, 4)
        payload["matchKinds"] = list(self.match_kinds)
        return payload

    def to_node(self) -> TextNode:
        return TextNode(
            text=f"《{self.law_name}》{self.article_no}：{self.content}",
            id_=self.provision_id,
            metadata={
                "law_name": self.law_name,
                "article_no": self.article_no,
                "doc_type": self.doc_type,
                "is_sample": self.is_sample,
                "match_kinds": ",".join(self.match_kinds),
            },
        )


class ProvisionRetriever(BaseRetriever):
    def __init__(
        self,
        session: AsyncSession,
        embedder: EmbeddingClient,
        app_settings: Settings,
    ) -> None:
        super().__init__()
        self._session = session
        self._embedder = embedder
        self._settings = app_settings

    # ---------- LlamaIndex 契约 ----------

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        """同步入口。检索依赖 asyncpg 会话，禁止在事件循环外同步调用。"""
        raise NotImplementedError(
            "ProvisionRetriever 只支持异步检索，请调用 `await retriever.aretrieve(...)`"
        )

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        provisions = await self.search(query_bundle.query_str)
        return [NodeWithScore(node=item.to_node(), score=item.score) for item in provisions]

    # ---------- 主流程 ----------

    async def search(
        self,
        query: str,
        *,
        refs: list[tuple[str, str | None]] | None = None,
        top_k: int | None = None,
        only_active: bool = True,
    ) -> list[RetrievedProvision]:
        """三路召回并融合。`refs` 为 (法规名, 条号或 None) 的直连需求。"""
        limit = top_k or self._settings.rag_top_k
        merged: dict[str, RetrievedProvision] = {}

        if refs:
            for item in await self._match_by_ref(refs, only_active=only_active):
                self._keep(merged, item, "exact_ref", 1.2)

        for item in await self._match_by_keyword(query, only_active=only_active):
            self._keep(merged, item, "keyword", 0.55 + item.score)

        try:
            for item in await self._match_by_vector(query, only_active=only_active):
                self._keep(merged, item, "vector", item.score)
        except Exception as exc:  # 向量分支失败不影响关键词与直连结果
            logger.warning("向量检索不可用，已降级为关键词召回：%s", exc)

        ranked = sorted(merged.values(), key=lambda item: item.score, reverse=True)[:limit]
        return ranked

    @staticmethod
    def _keep(merged: dict[str, RetrievedProvision], item: RetrievedProvision, kind: str, score: float) -> None:
        existing = merged.get(item.provision_id)
        if existing is None:
            item.score = score
            item.match_kinds = [kind]
            merged[item.provision_id] = item
            return
        existing.match_kinds.append(kind)
        # 多路命中要叠加可信度，但只取最高的那一路作为基础分值
        existing.score = max(existing.score, score) + 0.05

    async def _match_by_ref(
        self, refs: list[tuple[str, str | None]], *, only_active: bool
    ) -> list[RetrievedProvision]:
        stmt = select(LegalProvision)
        if only_active:
            stmt = stmt.where(LegalProvision.is_active)

        conditions = []
        for law_name, article_no in refs:
            if article_no:
                conditions.append(
                    (LegalProvision.law_name == law_name)
                    & (LegalProvision.article_no == article_no)
                )
            else:
                conditions.append(LegalProvision.law_name == law_name)
        if not conditions:
            return []

        from sqlalchemy import or_

        result = await self._session.execute(stmt.where(or_(*conditions)).limit(20))
        return [self._to_retrieved(row, score=1.0) for row in result.scalars().all()]

    async def _match_by_keyword(
        self, query: str, *, only_active: bool
    ) -> list[RetrievedProvision]:
        keywords = self._extract_keywords(query)
        article_refs = _ARTICLE_RE.findall(query)
        if not keywords and not article_refs:
            return []

        from sqlalchemy import or_

        clauses = []
        for keyword in keywords[:6]:
            clauses.append(LegalProvision.content.ilike(f"%{keyword}%"))
            clauses.append(LegalProvision.law_name.ilike(f"%{keyword}%"))
        for article in article_refs[:4]:
            clauses.append(LegalProvision.article_no == article)

        stmt = select(LegalProvision).where(or_(*clauses))
        if only_active:
            stmt = stmt.where(LegalProvision.is_active)

        result = await self._session.execute(stmt.limit(24))
        rows = list(result.scalars().all())

        scored: list[RetrievedProvision] = []
        for row in rows:
            hits = sum(1 for keyword in keywords[:6] if keyword in (row.content or ""))
            scored.append(self._to_retrieved(row, score=0.1 * hits))
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:8]

    async def _match_by_vector(
        self, query: str, *, only_active: bool
    ) -> list[RetrievedProvision]:
        if self._embedder is None:
            return []

        vector = await self._embedder.embed_query(query)
        distance = LegalProvision.embedding.cosine_distance(vector)
        stmt = (
            select(LegalProvision, distance.label("distance"))
            .where(LegalProvision.embedding.is_not(None))
            .order_by(distance.asc())
            .limit(self._settings.rag_top_k * 3)
        )
        if only_active:
            stmt = stmt.where(LegalProvision.is_active)

        result = await self._session.execute(stmt)
        items: list[RetrievedProvision] = []
        for row, raw_distance in result.all():
            similarity = 1.0 - float(raw_distance)
            if similarity < self._settings.rag_min_similarity:
                continue
            items.append(self._to_retrieved(row, score=similarity))
        return items

    # ---------- 辅助 ----------

    @staticmethod
    def _extract_keywords(query: str) -> list[str]:
        tokens = re.findall(r"[\u4e00-\u9fa5]{2,8}", query)
        unique: list[str] = []
        for token in tokens:
            if token in _KEYWORD_STOPWORDS or token in unique:
                continue
            unique.append(token)
        return unique

    @staticmethod
    def _to_retrieved(row: LegalProvision, *, score: float) -> RetrievedProvision:
        return RetrievedProvision(
            provision_id=str(row.id),
            law_name=row.law_name,
            article_no=row.article_no,
            content=row.content,
            doc_type=row.doc_type,
            is_sample=row.is_sample,
            score=score,
            effective_on=row.effective_on.isoformat() if row.effective_on else None,
            repealed_on=row.repealed_on.isoformat() if row.repealed_on else None,
            source_url=row.source_url,
        )
