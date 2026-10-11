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
import uuid
from dataclasses import dataclass, field

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.knowledge import LegalProvision
from app.services.rag.embeddings import EmbeddingClient
from app.services.rag.rerank import RerankClient, RerankUnavailable
from app.services.rag.sparse import SparseSearchIndex, SparseSearchUnavailable, atokenize

logger = get_logger(__name__)

#: RRF 平滑常数，经验值 60：名次靠后的差异被压缩，避免单通道第 1 名压倒一切
_RRF_K = 60
#: 红线规则声明的引用依据必须出现，给一个融合分盖过任何名次奖励的量级
_EXACT_REF_BONUS = 1.0

_ARTICLE_RE = re.compile(r"第[0-9一二三四五六七八九十百零〇]+条")

#: 送进 cross-encoder 的条文长度：再长就被截断，分数与文本不具对应关系
_RERANK_TEXT_LIMIT = 600


def _rerank_text(item: RetrievedProvision) -> str:
    return f"《{item.law_name}》{item.article_no}：{item.content[:_RERANK_TEXT_LIMIT]}"


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
        *,
        sparse: SparseSearchIndex | None = None,
        reranker: RerankClient | None = None,
    ) -> None:
        super().__init__()
        self._session = session
        self._embedder = embedder
        self._settings = app_settings
        self._sparse = sparse
        self._reranker = reranker
        #: 本次检索真正参与的通道，降级时不会被误报为“混合检索已启用”
        self.channels_used: list[str] = []

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
        """多通道召回 + RRF 融合。`refs` 为 (法规名, 条号或 None) 的直连需求。

        融合用 Reciprocal Rank Fusion 而不是分数加权：cosine 相似度与 BM25
        分值不同量纲、随数据分布漂移，名次则可以直接比较；多通道同时命中的
        条目自然累加，等价于交叉确认。
        """
        limit = top_k or self._settings.rag_top_k
        width = max(limit * 3, 12)
        self.channels_used = []

        by_id: dict[str, RetrievedProvision] = {}
        rankings: dict[str, list[str]] = {}

        if refs:
            exact = await self._match_by_ref(refs, only_active=only_active)
            if exact:
                self._collect(by_id, exact)
                rankings["exact_ref"] = [item.provision_id for item in exact]
                self.channels_used.append("exact_ref")

        try:
            dense = await self._match_by_vector(query, only_active=only_active, width=width)
            if dense:
                self._collect(by_id, dense)
                rankings["vector"] = [item.provision_id for item in dense]
                self.channels_used.append("vector")
        except Exception as exc:  # 向量分支失败不影响其余通道
            logger.warning("向量检索不可用，已降级：%s", exc)

        if self._sparse is not None:
            try:
                bm25 = await self._match_by_bm25(query, only_active=only_active, width=width)
                if bm25:
                    self._collect(by_id, bm25)
                    rankings["bm25"] = [item.provision_id for item in bm25]
                    self.channels_used.append("bm25")
            except SparseSearchUnavailable as exc:
                logger.warning("BM25 通道不可用，已退回其余通道：%s", exc)
            except Exception:
                # 非预期的错误（例如代码 bug）也要能降级不阻塞审查，但必须带栈日志，
                # 否则会被当成“ES 又挂了”长期掩盖
                logger.exception("BM25 通道异常（非连接问题，需排查）")

        # ILIKE 关键词既是 BM25 缺席时的兜底，也负责“第X条”这类字面条号命中
        keyword = await self._match_by_keyword(query, only_active=only_active)
        if keyword:
            self._collect(by_id, keyword)
            rankings["keyword"] = [item.provision_id for item in keyword]
            self.channels_used.append("keyword")

        fused = self._fuse(rankings)
        for provision_id, item in by_id.items():
            item.score = fused.get(provision_id, 0.0)
            item.match_kinds = [
                kind for kind, ranked in rankings.items() if provision_id in ranked
            ]

        ranked_items = sorted(
            by_id.values(),
            # 同分时非占位记录优先，避免合成文本挤掉真实原文
            key=lambda item: (item.score, not item.is_sample),
            reverse=True,
        )
        ranked_items = await self._rerank(query, ranked_items)
        return ranked_items[:limit]

    async def _rerank(self, query: str, items: list[RetrievedProvision]) -> list[RetrievedProvision]:
        """用 cross-encoder 重排并剔除低相关候选；不可用时原样退回 RRF 名次。

        直连命中（规则库声明的依据）不参与剔除：它不是“相似度发现”，而是
        规则自带的引用基础，进不了提示词会让模型看不到判定口径。
        """
        if self._reranker is None or not items:
            return items

        cap = max(1, self._settings.rag_rerank_max_candidates)
        candidates = items[:cap]
        pairs = [(item.provision_id, _rerank_text(item)) for item in candidates]

        try:
            outcome = await self._reranker.rerank(query, pairs)
        except RerankUnavailable as exc:
            logger.warning("重排服务不可用，沿用 RRF 名次：%s", exc)
            return items
        except Exception:
            logger.exception("重排异常（非连接问题，需排查），沿用 RRF 名次")
            return items

        if not outcome.applied:
            logger.info("重排后无候选达标：%s，仅保留直连命中", outcome.message)
        else:
            self.channels_used.append("rerank")

        kept: list[RetrievedProvision] = []
        for item in candidates:
            score = outcome.scores.get(item.provision_id)
            if score is not None:
                item.score = score
                item.match_kinds = [*item.match_kinds, "rerank"]
                kept.append(item)

        # 重排失败/被打分的直连命中回填，保证规则声明的依据不丢
        kept_ids = {item.provision_id for item in kept}
        protected = [
            item
            for item in candidates
            if "exact_ref" in item.match_kinds and item.provision_id not in kept_ids
        ]
        if protected and "exact_ref" not in self.channels_used:
            self.channels_used.append("exact_ref")
        if protected:
            kept = [*protected, *kept]

        logger.info(
            "重排结果：%s（候选 %s 条，输出 %s 条，另有 %s 条因属规则直连依据被保留）",
            outcome.message,
            len(candidates),
            len(kept) - len(protected),
            len(protected),
        )
        return kept

    @staticmethod
    def _collect(merged: dict[str, RetrievedProvision], items: list[RetrievedProvision]) -> None:
        """同一条文只留一份对象，后续统一赋值融合分。"""
        for item in items:
            merged.setdefault(item.provision_id, item)

    @staticmethod
    def _fuse(rankings: dict[str, list[str]]) -> dict[str, float]:
        """RRF：直连命中拿封顶分，其余通道按名次累加。"""
        scores: dict[str, float] = {}
        for provision_id in rankings.get("exact_ref", []):
            scores[provision_id] = scores.get(provision_id, 0.0) + _EXACT_REF_BONUS

        for kind, ranked in rankings.items():
            if kind == "exact_ref":
                continue
            for rank, provision_id in enumerate(ranked, start=1):
                scores[provision_id] = scores.get(provision_id, 0.0) + 1.0 / (_RRF_K + rank)
        return scores

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
        """ILIKE 兜底通道：与 BM25 共用同一套 jieba 词，不依赖 ES 也能拆出词组。

        旧实现用 `[\u4e00-\u9fa5]{2,8}` 连续抓取，拿到的是“借款的利率应当符”这类
        八字长串，ILIKE 包裹后几乎不可能命中；换成分词后才有召回能力。
        """
        keywords = [token for token in await atokenize(query) if len(token) >= 2]
        unique: list[str] = []
        for token in keywords:
            if token not in unique:
                unique.append(token)
        keywords = unique
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
        self, query: str, *, only_active: bool, width: int | None = None
    ) -> list[RetrievedProvision]:
        if self._embedder is None:
            return []

        vector = await self._embedder.embed_query(query)
        distance = LegalProvision.embedding.cosine_distance(vector)
        stmt = (
            select(LegalProvision, distance.label("distance"))
            .where(LegalProvision.embedding.is_not(None))
            .order_by(distance.asc())
            .limit(width or self._settings.rag_top_k * 3)
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

    async def _match_by_bm25(
        self, query: str, *, only_active: bool, width: int | None = None
    ) -> list[RetrievedProvision]:
        """ES 名次为输入，回到主表取权威文本，保证引用能直接回连条文主键。"""
        if self._sparse is None:
            return []

        hits = await self._sparse.search(
            query,
            size=max(width or 0, self._settings.rag_sparse_candidate_size),
            only_active=only_active,
        )
        if not hits:
            return []

        ordered_ids = [provision_id for provision_id, _ in hits]
        rows = await self._load_by_ids(ordered_ids)
        items: list[RetrievedProvision] = []
        for provision_id in ordered_ids:
            row = rows.get(provision_id)
            if row is not None:
                items.append(self._to_retrieved(row, score=0.0))
        return items

    async def _load_by_ids(self, provision_ids: list[str]) -> dict[str, LegalProvision]:
        valid: list[uuid.UUID] = []
        for raw in provision_ids:
            try:
                valid.append(uuid.UUID(raw))
            except (ValueError, TypeError):
                logger.warning("BM25 返回了非 UUID 主键，已丢弃：%s", raw)
        if not valid:
            return {}

        result = await self._session.execute(select(LegalProvision).where(LegalProvision.id.in_(valid)))
        return {str(row.id): row for row in result.scalars().all()}

    # ---------- 辅助 ----------

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
