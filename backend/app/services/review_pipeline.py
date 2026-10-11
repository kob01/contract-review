"""审查管道：规则引擎 + LLM/RAG 双通道编排。

执行顺序即设计意图：

1. 规则引擎先跑完，得到确定性硬结论与"无法定量判定"的规则清单；
2. 按批次把条款原文送入模型，检索到的法条与规则结论一起进提示词；
3. 模型结论逐条做原文定位与引用核验，定位不到或引用不上的都降级处理；
4. 两条通道交叉校验，冲突以规则引擎为准；
5. 按置信度与核验结果路由，未达标的进人工复核队列；
6. 全部结论、指标快照、复核任务一次性落库。

任何一步失败都不会被"吞掉后继续出报告"：失败批次计入 `failed_batch_count`，
整体失败则审查状态置为 failed，绝不用半截结果冒充完成。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.contract import Contract
from app.models.enums import (
    RISK_SCORE_PENALTY,
    ContractStatus,
    CrossCheckStatus,
    FindingSource,
    HumanReviewStatus,
    ReviewStatus,
    RiskLevel,
)
from app.models.human_review import HumanReviewTask
from app.models.redline import RedLineRule
from app.models.review import ContractReview, ReviewFinding
from app.schemas.review import ReviewBatchDraft, ReviewFindingDraft, ReviewRunRequest
from app.services.citation import resolve_citations
from app.services.findings import (
    CandidateFinding,
    apply_judge_verdicts,
    apply_routing,
    merge_and_cross_check,
)
from app.services.judge import run_judge
from app.services.llm.base import LLMError, LLMProvider
from app.services.llm.prompts import (
    PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
    describe_pending_rules,
    describe_rule_hits_for_prompt,
)
from app.services.parser.segmenter import batch_segments, locate_excerpt
from app.services.rag.embeddings import EmbeddingClient
from app.services.rag.rerank import RerankClient
from app.services.rag.retriever import ProvisionRetriever, RetrievedProvision
from app.services.rag.sparse import SparseSearchIndex
from app.services.rules.engine import RuleEngine, RuleHit
from app.services.rules.metrics import MetricSet, metric_summary_for_prompt
from app.services.rules.seed import load_active_rules, rules_for_prompt

logger = get_logger(__name__)

#: 进提示词的条文上限，再多就会挤占条款原文预算并稀释注意力
_MAX_PROVISIONS_IN_PROMPT = 14


class ReviewPipelineError(RuntimeError):
    """整次审查无法完成。"""


class ReviewPipeline:
    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        embedder: EmbeddingClient,
        app_settings: Settings,
        *,
        sparse: SparseSearchIndex | None = None,
        reranker: RerankClient | None = None,
        judge: LLMProvider | None = None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._embedder = embedder
        self._settings = app_settings
        self._sparse = sparse
        self._reranker = reranker
        self._judge = judge

    # ---------- 对外入口 ----------

    async def run(self, review_id: uuid.UUID, request: ReviewRunRequest) -> ContractReview:
        review = await self._session.get(ContractReview, review_id)
        if review is None:
            raise ReviewPipelineError(f"审查任务 {review_id} 不存在")

        contract = await self._session.get(Contract, review.contract_id)
        if contract is None:
            raise ReviewPipelineError("关联合同已被删除")

        started = time.perf_counter()
        self._mark_running(review, contract, request)
        await self._session.commit()

        try:
            outcome = await self._analyze(contract, request)
        except Exception as exc:
            await self._mark_failed(review, contract, exc, started)
            raise

        await self._persist(review, contract, outcome, started)
        await self._session.commit()
        logger.info(
            "审查完成 contract=%s review=%s 结论=%s 待复核=%s",
            contract.id,
            review.id,
            len(outcome["items"]),
            review.pending_human_count,
        )
        return review

    # ---------- 阶段 0：状态 ----------

    def _mark_running(
        self, review: ContractReview, contract: Contract, request: ReviewRunRequest
    ) -> None:
        review.status = ReviewStatus.RUNNING.value
        review.llm_model = self._provider.model
        review.embedding_model = f"{self._embedder.name}:{self._embedder.dimensions}"
        #: 空串是有意保留的事实：本次没有独立复核模型
        review.judge_model = self._judge.model if self._judge is not None else ""
        review.prompt_version = PROMPT_VERSION
        review.run_options = request.model_dump(mode="json", by_alias=True)
        review.started_at = datetime.now(UTC)
        review.error_message = None
        contract.status = ContractStatus.REVIEWING.value

    async def _mark_failed(
        self,
        review: ContractReview,
        contract: Contract,
        exc: Exception,
        started: float,
    ) -> None:
        review.status = ReviewStatus.FAILED.value
        review.error_message = f"{type(exc).__name__}: {exc}"
        review.finished_at = datetime.now(UTC)
        review.elapsed_seconds = round(time.perf_counter() - started, 3)
        contract.status = ContractStatus.FAILED.value
        await self._session.commit()
        logger.error("审查失败 review=%s：%s", review.id, exc)

    # ---------- 阶段 1~4 ----------

    async def _analyze(self, contract: Contract, request: ReviewRunRequest) -> dict:
        metrics = MetricSet.from_dict(contract.extracted_metrics)
        segments = list(contract.segments or [])
        full_text = contract.content_text or ""

        if not segments or not full_text:
            raise ReviewPipelineError("合同尚未解析完成，无法审查")

        rules = await load_active_rules(self._session)
        if not rules:
            logger.warning("红线库为空，规则通道无法参与交叉校验")

        evaluation = RuleEngine(rules, self._settings).evaluate(
            metrics,
            full_text=full_text,
            segments=segments,
            contract_kind=contract.contract_kind,
        )

        rule_items = [
            await self._rule_candidate(hit, segments, full_text) for hit in evaluation.hits
        ]

        pending_rules = await rules_for_prompt(
            self._session, [code for code, _ in evaluation.inconclusive]
        )
        weak_rules = await rules_for_prompt(self._session, evaluation.weak_signals)

        llm_items, stats = await self._llm_channel(
            contract=contract,
            request=request,
            segments=segments,
            metrics=metrics,
            rule_items=rule_items,
            focus_rules=[*pending_rules, *weak_rules],
        )

        merged, conflicts = merge_and_cross_check(rule_items, llm_items)
        apply_routing(
            merged,
            threshold=self._settings.confidence_routing_threshold,
            mandatory_high_risk=self._settings.mandatory_human_review_for_high_risk,
        )

        # 复核在路由之后跑：它只能加严（把该人工的拉进队列），不能减严
        judge_outcome = await run_judge(self._judge, merged, stats["retrieved"], self._settings)
        judge_counts = apply_judge_verdicts(
            merged,
            judge_outcome.verdicts,
            judge_model=judge_outcome.model,
            min_confidence=self._settings.judge_min_confidence,
        )

        return {
            "items": merged,
            "conflicts": conflicts,
            "rule_hits": len(evaluation.hits),
            "hard_hits": len(evaluation.hard_hits),
            "retrieved": stats["retrieved"],
            "channels": stats["channels"],
            "llm_calls": stats["calls"],
            "failed_batches": stats["failed"],
            "judge_calls": judge_outcome.calls,
            "judge_failed": judge_outcome.failed_batches,
            "judge_counts": judge_counts,
            "inconclusive": evaluation.inconclusive,
            "batch_total": stats["batches"],
        }

    # ---------- 规则通道 ----------

    async def _rule_candidate(
        self, hit: RuleHit, segments: list[dict], full_text: str
    ) -> CandidateFinding:
        citations, _ = await resolve_citations(self._session, hit.basis_refs)

        position = None
        if hit.evidence is not None and hit.evidence.excerpt:
            position = locate_excerpt(segments, full_text, hit.evidence.excerpt)

        return CandidateFinding(
            title=hit.title,
            risk_level=hit.severity,
            clause_category=hit.category,
            analysis=hit.analysis,
            suggestion=hit.suggestion,
            excerpt=(hit.evidence.excerpt if hit.evidence else hit.title),
            source=FindingSource.RULE.value,
            cross_check_status=CrossCheckStatus.RULE_ONLY.value,
            confidence=hit.confidence,
            rule_confidence=hit.confidence,
            rule_code=hit.rule_code,
            metric_snapshot=hit.metric_snapshot,
            rewritten_text=None,
            segment_index=(position or {}).get("segment_index")
            or (hit.evidence.segment_index if hit.evidence else None),
            page_no=(position or {}).get("page_no") or (hit.evidence.page_no if hit.evidence else None),
            char_start=(position or {}).get("char_start")
            or (hit.evidence.char_start if hit.evidence else None),
            char_end=(position or {}).get("char_end")
            or (hit.evidence.char_end if hit.evidence else None),
            citations=citations,
            hard_block=hit.hard_block,
            located=position is not None or hit.evidence is not None,
        )

    # ---------- LLM + RAG 通道 ----------

    async def _llm_channel(
        self,
        *,
        contract: Contract,
        request: ReviewRunRequest,
        segments: list[dict],
        metrics: MetricSet,
        rule_items: list[CandidateFinding],
        focus_rules: list[RedLineRule],
    ) -> tuple[list[CandidateFinding], dict]:
        stats = {"retrieved": [], "calls": 0, "failed": 0, "batches": 0, "channels": []}
        if request.rule_only:
            return [], stats

        batches = batch_segments(segments, self._settings.llm_max_chars_per_call)
        stats["batches"] = len(batches)
        metric_text = metric_summary_for_prompt(metrics)
        hit_lines = describe_rule_hits_for_prompt([_FakeHit(item) for item in rule_items])
        pending_lines = describe_pending_rules(focus_rules, metric_text)

        # 1) 逐批检索，累积去重后的条文池
        pool: dict[str, RetrievedProvision] = {}
        retriever = ProvisionRetriever(
            self._session,
            self._embedder,
            self._settings,
            sparse=self._sparse,
            reranker=self._reranker,
        )
        basis_refs = [
            (str(ref.get("law_name")), ref.get("article_no"))
            for rule in focus_rules
            for ref in (rule.basis_refs or [])
            if ref.get("law_name")
        ]

        for batch in batches:
            query = _retrieval_query(batch, contract.contract_kind, focus_rules)
            for item in await retriever.search(query, refs=basis_refs, top_k=request.top_k):
                pool.setdefault(item.provision_id, item)

        stats["channels"] = retriever.channels_used
        stats["retrieved"] = list(pool.values())

        # 2) 并发调用模型（此阶段不碰数据库，AsyncSession 不是并发安全的）
        semaphore = asyncio.Semaphore(max(1, self._settings.llm_max_concurrency))

        async def invoke(index: int, batch: list[dict]):
            provisions = _rank_provisions(list(pool.values()), batch)[
                :_MAX_PROVISIONS_IN_PROMPT
            ]
            system_prompt = build_system_prompt(
                metric_text=metric_text,
                provisions=provisions,
                rule_hits=hit_lines,
                rule_pending=pending_lines,
                request=request,
            )
            user_prompt = build_user_prompt(batch, batch_no=index, batch_total=len(batches))
            async with semaphore:
                stats["calls"] += 1
                try:
                    result = await self._provider.complete_json(
                        system_prompt=system_prompt, user_prompt=user_prompt
                    )
                    return ReviewBatchDraft.model_validate(result.payload), batch, None
                except (LLMError, ValidationError) as exc:
                    return None, batch, exc

        results = await asyncio.gather(*(invoke(i, b) for i, b in enumerate(batches, 1)))

        # 3) 顺序做引用核验与原文定位
        full_text = contract.content_text or ""
        candidates: list[CandidateFinding] = []
        for draft, batch, error in results:
            if error is not None or draft is None:
                stats["failed"] += 1
                logger.warning("批次 %s 模型输出无效：%s", batch[0].get("index"), error)
                continue
            for finding in draft.findings:
                candidates.append(
                    await self._llm_candidate(finding, batch, segments, full_text)
                )

        return candidates, stats

    async def _llm_candidate(
        self,
        finding: ReviewFindingDraft,
        batch: list[dict],
        segments: list[dict],
        full_text: str,
    ) -> CandidateFinding:
        position = locate_excerpt(segments, full_text, finding.excerpt)
        if position is None and finding.segment_index is not None:
            # 退一步：至少归到模型指认的条款上，但标记为未精确定位
            position = _segment_position(segments, finding.segment_index)

        citations, _ = await resolve_citations(
            self._session,
            [item.model_dump(mode="json", by_alias=True) for item in finding.citations],
        )

        return CandidateFinding(
            title=finding.title,
            risk_level=finding.risk_level.value,
            clause_category=finding.clause_category.value,
            analysis=finding.analysis,
            suggestion=finding.suggestion,
            rewritten_text=finding.rewritten_text,
            excerpt=finding.excerpt,
            source=FindingSource.LLM.value,
            cross_check_status=CrossCheckStatus.LLM_ONLY.value,
            confidence=max(0.0, min(1.0, finding.confidence)),
            llm_confidence=max(0.0, min(1.0, finding.confidence)),
            rule_code=finding.rule_code,
            segment_index=(position or {}).get("segment_index", finding.segment_index),
            page_no=(position or {}).get("page_no"),
            char_start=(position or {}).get("char_start"),
            char_end=(position or {}).get("char_end"),
            citations=citations,
            located=position is not None,
        )

    # ---------- 落库 ----------

    async def _persist(self, review: ContractReview, contract: Contract, outcome: dict, started: float) -> None:
        items: list[CandidateFinding] = sorted(
            outcome["items"],
            key=lambda item: (RiskLevel(item.risk_level).weight, item.confidence),
            reverse=True,
        )

        high = medium = low = 0
        pending = 0
        for index, item in enumerate(items):
            if item.risk_level == RiskLevel.HIGH.value:
                high += 1
            elif item.risk_level == RiskLevel.MEDIUM.value:
                medium += 1
            elif item.risk_level == RiskLevel.LOW.value:
                low += 1

            finding = ReviewFinding(
                review_id=review.id,
                risk_level=item.risk_level,
                clause_category=item.clause_category,
                title=item.title[:255],
                excerpt=item.excerpt,
                analysis=item.analysis,
                suggestion=item.suggestion,
                rewritten_text=item.rewritten_text,
                source=item.source,
                cross_check_status=item.cross_check_status,
                rule_code=item.rule_code,
                metric_snapshot=item.metric_snapshot,
                confidence=item.confidence,
                llm_confidence=item.llm_confidence,
                rule_confidence=item.rule_confidence,
                segment_index=item.segment_index,
                page_no=item.page_no,
                char_start=item.char_start,
                char_end=item.char_end,
                citations=item.citations,
                judge_verdict=item.judge_verdict,
                human_review_status=item.human_review_status,
                order_index=index,
            )
            self._session.add(finding)

            if item.human_review_status == HumanReviewStatus.PENDING.value:
                # 主键由 flush 时才生成，不先 flush 就拿不到 finding.id
                await self._session.flush()
                pending += 1
                self._session.add(
                    HumanReviewTask(
                        finding_id=finding.id,
                        review_id=review.id,
                        contract_id=contract.id,
                        reason=item.human_review_reason or "manual",
                        status=HumanReviewStatus.PENDING.value,
                        model_snapshot={
                            "title": item.title,
                            "excerpt": item.excerpt,
                            "analysis": item.analysis,
                            "confidence": item.confidence,
                            "source": item.source,
                            "citations": item.citations,
                            "promptVersion": review.prompt_version,
                            "llmModel": review.llm_model,
                        },
                    )
                )

        penalty = sum(RISK_SCORE_PENALTY[RiskLevel(item.risk_level)] for item in items)
        review.risk_score = max(0, 100 - penalty)
        review.high_risk_count = high
        review.medium_risk_count = medium
        review.low_risk_count = low
        review.rule_hit_count = outcome["rule_hits"]
        review.llm_finding_count = len(
            [item for item in items if item.source in (FindingSource.LLM.value, FindingSource.BOTH.value)]
        )
        review.conflict_count = outcome["conflicts"]
        review.retrieved_chunk_count = len(outcome["retrieved"])
        review.retrieved_provisions = [
            item.to_snapshot() for item in outcome["retrieved"]
        ]
        review.llm_call_count = outcome["llm_calls"]
        review.failed_batch_count = outcome["failed_batches"]
        review.judge_call_count = outcome["judge_calls"]
        review.judge_failed_count = outcome["judge_failed"]
        review.retrieval_channels = outcome["channels"]
        review.min_confidence = round(min((item.confidence for item in items), default=1.0), 4)
        review.pending_human_count = pending
        review.needs_human_review = pending > 0
        review.summary = _build_summary(review, items, outcome)
        review.status = ReviewStatus.SUCCEEDED.value
        review.finished_at = datetime.now(UTC)
        review.elapsed_seconds = round(time.perf_counter() - started, 3)
        contract.status = ContractStatus.REVIEWED.value


class _FakeHit:
    """把已生成的规则结论适配成提示词描述所需的结构。"""

    def __init__(self, item: CandidateFinding) -> None:
        self.rule_code = item.rule_code or ""
        self.rule_name = item.title
        self.analysis = item.analysis
        self.threshold_desc = str(item.metric_snapshot.get("thresholdDesc") or "")


def _retrieval_query(batch: list[dict], contract_kind: str, rules: list[RedLineRule]) -> str:
    parts = [f"{contract_kind} 合同", "综合年化融资成本 利率明示 费用", "格式条款 消费者保护"]
    parts.extend((segment.get("title") or "") for segment in batch[:6])
    parts.extend(rule.name for rule in rules[:6])
    return " ".join(part for part in parts if part).strip()


def _rank_provisions(items: list[RetrievedProvision], batch: list[dict]) -> list[RetrievedProvision]:
    """把与本轮条款更相关的条文排在前面，避免占位记录挤占提示词窗口。"""
    keywords = " ".join((segment.get("title") or "") + (segment.get("text") or "")[:200] for segment in batch)

    def score(item: RetrievedProvision) -> tuple:
        lexical = sum(1 for token in item.law_name if token in keywords)
        return (0 if item.is_sample else 1, item.score + 0.01 * lexical)

    return sorted(items, key=score, reverse=True)


def _segment_position(segments: list[dict], index: int) -> dict | None:
    for segment in segments:
        if segment.get("index") == index:
            return {
                "segment_index": index,
                "char_start": segment.get("char_start"),
                "char_end": segment.get("char_end"),
                "page_no": segment.get("page_no"),
            }
    return None


def _build_summary(review: ContractReview, items: list[CandidateFinding], outcome: dict) -> str:
    placeholder = sum(
        1
        for item in items
        for citation in item.citations
        if citation.get("is_sample") or not citation.get("verified", False)
    )
    parts = [
        f"规则引擎命中 {review.rule_hit_count} 条，模型补充 {review.llm_finding_count} 条，"
        f"交叉校验冲突 {review.conflict_count} 条。",
        f"风险评分 {review.risk_score}/100（高 {review.high_risk_count} / "
        f"中 {review.medium_risk_count} / 低 {review.low_risk_count}）。",
    ]
    if review.failed_batch_count:
        parts.append(f"有 {review.failed_batch_count} 个批次模型输出无效，已跳过。")

    counts = outcome.get("judge_counts") or {}
    if not review.judge_model:
        parts.append("本次未执行独立结论复核（未配置复核模型），置信度路由仍按原口径生效。")
    else:
        judge_line = (
            f"复核模型 {review.judge_model} 已复核 {counts.get('judged', 0)} 条，"
            f"其中 {counts.get('disputed', 0)} 条被判为不成立或把握不足。"
        )
        if review.judge_failed_count:
            judge_line += f" 另有 {review.judge_failed_count} 批复核调用失败，相关结论标记为未复核。"
        parts.append(judge_line)

    channels = outcome.get("channels") or []
    if channels:
        parts.append("本次召回参与通道：" + "、".join(channels) + "。")
    if placeholder:
        parts.append(
            f"存在 {placeholder} 处引用依据未核验（多为法条原文尚未导入），"
            "结论不可直接对外使用。"
        )
    if review.pending_human_count:
        parts.append(f"{review.pending_human_count} 条结论已流转人工法务复核。")
    else:
        parts.append("全部结论均满足自动通过条件（规则硬命中且引用已核验）。")
    del outcome
    return " ".join(parts)
