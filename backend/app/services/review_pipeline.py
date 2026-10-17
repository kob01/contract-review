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
    ModelSlot,
    ReviewStatus,
    RiskLevel,
)
from app.models.human_review import HumanReviewTask
from app.models.redline import RedLineRule
from app.models.review import ContractReview, ReviewFinding
from app.models.telemetry import ReviewLlmCall, ReviewRetrievalEvent
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
from app.services.llm.factory import RULE_CHECK_MODEL, JudgeSlot
from app.services.llm.prompts import (
    PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
    describe_pending_rules,
    describe_rule_hits_for_prompt,
    detect_injection_attempts,
    restore_untrusted,
)
from app.services.parser.segmenter import batch_segments, locate_excerpt
from app.services.rag.embeddings import EmbeddingClient
from app.services.rag.rerank import RerankClient
from app.services.rag.retriever import (
    ProvisionRetriever,
    RetrievedCase,
    RetrievedProvision,
    build_token_index,
    lexical_overlap,
)
from app.services.rag.sparse import SparseSearchIndex, atokenize
from app.services.rules.engine import RuleEngine, RuleHit
from app.services.rules.metrics import MetricSet, metric_summary_for_prompt
from app.services.rules.seed import load_active_rules, rules_for_prompt
from app.services.rules.thresholds import Thresholds, load_thresholds
from app.services.telemetry import (
    DegradationLog,
    StageTimer,
    citation_verification_status,
    config_fingerprint,
    locate_method_name,
    summarize_token_usage,
)

logger = get_logger(__name__)


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
        judge_slot: JudgeSlot | None = None,
        session_factory=None,
    ) -> None:
        self._session = session
        self._provider = provider
        self._embedder = embedder
        self._settings = app_settings
        self._sparse = sparse
        self._reranker = reranker
        #: 复核不再只是一个供应商对象：它带“本次到底复核了没有、是不是独立信号”
        self._judge_slot = judge_slot or JudgeSlot()
        #: 逐批检索需要多个并发会话，而 AsyncSession 不是并发安全的，
        #: 因此每批借一个侧会话（延迟读取，避免导入时拉起引擎）
        self._session_factory = session_factory

        # ---------- 评测埋点（不参与业务判定，写入失败不得影响审查）----------
        self._timer = StageTimer()
        self._degradations = DegradationLog()
        #: 逐次模型调用留痕：主审与复核分槽，token/延时/失败原因各自记账
        self._llm_records: list[dict] = []
        #: 逐条检索过程留痕：融合前名次、精排分、是否进提示词
        self._retrieval_events: list[dict] = []

    @property
    def _side_factory(self):
        if self._session_factory is not None:
            return self._session_factory
        from app.db.session import SessionLocal

        return SessionLocal

    # ---------- 对外入口 ----------

    async def run(self, review_id: uuid.UUID, request: ReviewRunRequest) -> ContractReview:
        review = await self._session.get(ContractReview, review_id)
        if review is None:
            raise ReviewPipelineError(f"审查任务 {review_id} 不存在")

        contract = await self._session.get(Contract, review.contract_id)
        if contract is None:
            raise ReviewPipelineError("关联合同已被删除")

        started = time.perf_counter()
        #: 一个管道实例可能被复用（worker 复用、脚本连跑），埋点必须每次清空，
        #: 否则上一份合同的调用记录会串到这一份的账上
        self._timer = StageTimer()
        self._degradations = DegradationLog()
        self._llm_records = []
        self._retrieval_events = []
        self._mark_running(review, contract, request)
        await self._session.commit()

        try:
            outcome = await self._analyze(contract, request)
        except Exception as exc:
            await self._mark_failed(review, contract, exc, started)
            # 失败样本恰恰是最需要过程数据的（哪一批炸的、思考 token 撞没撞上限），
            # 所以埋点照写；它走独立事务，写不上也不会改动这次的失败结论
            await self._persist_telemetry(review.id)
            raise

        await self._persist(review, contract, outcome, started)
        await self._session.commit()
        await self._persist_telemetry(review.id)
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
        #: 空串是有意保留的事实：本次没有独立复核通道；
        #: `rule-check-v1` 则表示“没叫模型，但做了确定性复核”
        review.judge_model = self._judge_slot.model
        review.prompt_version = PROMPT_VERSION
        #: 思考档位属配置而不是请求参数，两次运行可能不同：与模型名一样固化成快照，
        #: 事后才能回答“这条结论是在哪一档思考深度下出的、当时复核有没有真的跑”
        options = request.model_dump(mode="json", by_alias=True)
        options["thinkingDepth"] = {
            "review": self._provider.reasoning_effort,
            "judge": (
                self._judge_slot.provider.reasoning_effort
                if self._judge_slot.provider is not None
                else ""
            ),
        }
        #: 复核形态与是否同源必须固化：否则“glm 自己复核自己”与
        #: “DeepSeek 复核 glm”在历史报告里会长得一模一样
        options["judgeMode"] = self._judge_slot.mode
        options["judgeIndependent"] = self._judge_slot.independent
        #: 评测可复现锚（DAT-05）：模型、阈值口径、检索参数全部固化进本次记录，
        #: 否则两次跑分的差异分不清是代码变了还是配置漂移了
        fingerprint = config_fingerprint(self._settings)
        fingerprint["llm"]["promptVersion"] = PROMPT_VERSION
        options["configFingerprint"] = fingerprint
        review.run_options = options
        review.started_at = datetime.now(UTC)
        review.heartbeat_at = datetime.now(UTC)
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

        with self._timer.stage("rules_load"):
            rules = await load_active_rules(self._session)
            if not rules:
                self._degrade("redline_library_empty", "红线库为空，规则通道无法参与交叉校验")
            # 阈值口径从库里的生效值读（法务可自助调整），不能只盯环境变量；
            # 用的具体数值会归入本次审查快照，事后能回答“当时按哪个口径判的”
            thresholds = await load_thresholds(self._session, self._settings)

        with self._timer.stage("rule_engine"):
            evaluation = RuleEngine(rules, self._settings, thresholds).evaluate(
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

        # 合同正文是外部输入：先扫一轮“让审查系统忽略规则”这类注入措辞。
        # 不丢弃正文（正文本身可能就是规避证据），但必须让运维看得到
        injection_hits = detect_injection_attempts(full_text)
        if injection_hits:
            self._degrade(
                "injection_signals",
                f"正文含疑似提示注入措辞 {len(injection_hits)} 处（仅记录，不删除原文）："
                + " | ".join(injection_hits),
            )

        llm_items, stats = await self._llm_channel(
            contract=contract,
            request=request,
            segments=segments,
            metrics=metrics,
            rule_items=rule_items,
            focus_rules=[*pending_rules, *weak_rules],
        )

        with self._timer.stage("merge_routing"):
            merged, conflicts = merge_and_cross_check(rule_items, llm_items)
            apply_routing(
                merged,
                threshold=thresholds.confidence_routing_threshold,
                mandatory_high_risk=self._settings.mandatory_human_review_for_high_risk,
            )

        # 复核在路由之后跑：它只能加严（把该人工的拉进队列），不能减严
        with self._timer.stage("judge"):
            judge_outcome = await run_judge(
                self._judge_slot, merged, stats["retrieved"], self._settings
            )
            self._llm_records.extend(judge_outcome.calls_detail)
            judge_counts = apply_judge_verdicts(
                merged,
                judge_outcome.verdicts,
                judge_model=judge_outcome.model,
                min_confidence=self._settings.judge_min_confidence,
            )
        if judge_outcome.failed_batches:
            self._degrade(
                "judge_batches_failed",
                f"{judge_outcome.failed_batches}/{judge_outcome.calls} 批复核失败，"
                "相关结论标记为未复核",
            )
        if judge_outcome.model == RULE_CHECK_MODEL:
            self._degrade("judge_downgraded_to_rule_check", "主审与复核同模型，改用确定性规则复核")
        if judge_counts.get("unjudged"):
            self._degrade("judge_unjudged", f"{judge_counts['unjudged']} 条结论没有拿到裁定")

        return {
            "items": merged,
            "conflicts": conflicts,
            "rule_hits": len(evaluation.hits),
            "hard_hits": len(evaluation.hard_hits),
            "retrieved": stats["retrieved"],
            "cases": stats.get("cases", []),
            "channels": stats["channels"],
            "llm_calls": stats["calls"],
            "failed_batches": stats["failed"],
            "batch_failures": stats.get("failures", []),
            "judge_calls": judge_outcome.calls,
            "judge_failed": judge_outcome.failed_batches,
            "judge_counts": judge_counts,
            "inconclusive": evaluation.inconclusive,
            "batch_total": stats["batches"],
            "injection_hits": injection_hits,
            "thresholds": thresholds,
        }

    def _degrade(self, code: str, detail: str = "") -> None:
        """记录一处降级：既进日志也进本次审查的留痕字段。

        只在日志里写一行 warning，等于降级不可统计（ORCH-06）；
        审查记录里必须能回答"这次降了几级、降在哪一步"。
        """
        self._degradations.add(code, detail)
        logger.warning("降级留痕 [%s]：%s", code, detail)

    # ---------- 规则通道 ----------

    async def _rule_candidate(
        self, hit: RuleHit, segments: list[dict], full_text: str
    ) -> CandidateFinding:
        citations, _ = await resolve_citations(self._session, hit.basis_refs)

        position = None
        if hit.evidence is not None and hit.evidence.excerpt:
            position = locate_excerpt(segments, full_text, hit.evidence.excerpt)

        located = position is not None or hit.evidence is not None
        if not located:
            self._degrade("rule_hit_not_located", f"{hit.rule_code} 的命中证据无法回连合同原文")

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
            located=located,
            # 规则通道的偏移优先来自抽取时的证据本身，与模型 excerpt 回连是两条链路
            locate_method=(position or {}).get("method") or ("evidence" if hit.evidence else "failed"),
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
        stats = {
            "retrieved": [],
            "cases": [],
            "calls": 0,
            "failed": 0,
            "batches": 0,
            "channels": [],
            #: 每批失败的原因，落 `review_llm_calls` 之外也留在 stats 里供报告与评测直接取用
            "failures": [],
        }
        if request.rule_only:
            return [], stats

        batches = batch_segments(segments, self._settings.llm_max_chars_per_call)
        stats["batches"] = len(batches)
        metric_text = metric_summary_for_prompt(metrics)
        hit_lines = describe_rule_hits_for_prompt([_FakeHit(item) for item in rule_items])
        pending_lines = describe_pending_rules(focus_rules, metric_text)
        basis_refs = [
            (str(ref.get("law_name")), ref.get("article_no"))
            for rule in focus_rules
            for ref in (rule.basis_refs or [])
            if ref.get("law_name")
        ]

        # 1) 逐批检索（并发，每批一个侧会话），累积去重后的条文池
        with self._timer.stage("retrieval"):
            pool, case_pool, channels, retrieval_events = await self._retrieve_for_batches(
                batches,
                contract=contract,
                focus_rules=focus_rules,
                basis_refs=basis_refs,
                top_k=request.top_k,
            )
        self._retrieval_events.extend(retrieval_events)
        stats["channels"] = channels
        stats["retrieved"] = list(pool.values())
        stats["cases"] = list(case_pool.values())

        # 2) 词面索引与批次词集预计算：旧实现逐字比对法规名，每批重做一遍分词
        token_index = await build_token_index(pool.values())
        batch_tokens = [
            {token for token in tokens if len(token) >= 2}
            for tokens in await asyncio.gather(
                *(atokenize(_batch_text(batch)) for batch in batches)
            )
        ]

        # 3) 并发调用模型（此阶段不碰数据库，AsyncSession 不是并发安全的）
        isolate = self._settings.prompt_isolate_untrusted
        provision_cap = max(1, self._settings.max_provisions_in_prompt)
        cases_for_prompt = list(case_pool.values())
        semaphore = asyncio.Semaphore(max(1, self._settings.llm_max_concurrency))

        async def invoke(index: int, batch: list[dict]):
            provisions = _rank_provisions(
                list(pool.values()), batch_tokens[index - 1], token_index
            )[:provision_cap]
            system_prompt = build_system_prompt(
                metric_text=metric_text,
                provisions=provisions,
                rule_hits=hit_lines,
                rule_pending=pending_lines,
                request=request,
                cases=cases_for_prompt,
            )
            user_prompt = build_user_prompt(
                batch, batch_no=index, batch_total=len(batches), isolate=isolate
            )
            record = {
                "slot": ModelSlot.REVIEW.value,
                "batchNo": index,
                "model": self._provider.model,
                "provider": self._provider.name,
                "reasoningEffort": getattr(self._provider, "reasoning_effort", "") or "default",
                "promptVersion": PROMPT_VERSION,
                "charsIn": len(system_prompt) + len(user_prompt),
                "segmentIndexes": [int(seg.get("index") or 0) for seg in batch],
                "provisionsInPrompt": [item.provision_id for item in provisions],
                "ok": False,
            }
            started = time.perf_counter()
            async with semaphore:
                stats["calls"] += 1
                try:
                    result = await self._provider.complete_json(
                        system_prompt=system_prompt, user_prompt=user_prompt
                    )
                    draft = ReviewBatchDraft.model_validate(result.payload)
                    record.update(
                        {
                            "ok": True,
                            "latencySeconds": result.latency_seconds
                            or round(time.perf_counter() - started, 3),
                            "promptTokens": result.usage.prompt_tokens,
                            "completionTokens": result.usage.completion_tokens,
                            "totalTokens": result.usage.total_tokens,
                            "reasoningTokens": result.usage.reasoning_tokens,
                            "finishReason": result.finish_reason,
                            "charsOut": len(result.raw_content or ""),
                            "findingCount": len(draft.findings),
                            "rawContent": result.raw_content or "",
                        }
                    )
                    return draft, batch, None, record
                except (LLMError, ValidationError) as exc:
                    record.update(
                        {
                            "latencySeconds": round(time.perf_counter() - started, 3),
                            "errorType": type(exc).__name__,
                            "errorMessage": str(exc)[:1000],
                        }
                    )
                    return None, batch, exc, record

        with self._timer.stage("llm"):
            results = await asyncio.gather(*(invoke(i, b) for i, b in enumerate(batches, 1)))

        # 4) 顺序做引用核验与原文定位
        full_text = contract.content_text or ""
        valid_case_ids = set(case_pool)
        candidates: list[CandidateFinding] = []
        for batch, (draft, _batch, error, record) in zip(batches, results, strict=True):
            self._llm_records.append(record)
            if error is not None or draft is None:
                stats["failed"] += 1
                stats["failures"].append(
                    {
                        "batchNo": record.get("batchNo"),
                        "errorType": record.get("errorType", ""),
                        "errorMessage": record.get("errorMessage", ""),
                    }
                )
                self._degrade(
                    "llm_batch_invalid",
                    f"第 {record.get('batchNo')} 批（条款 {_batch[0].get('index')} 起）"
                    f"输出无效：{record.get('errorType')}",
                )
                logger.warning("批次 %s 模型输出无效：%s", _batch[0].get("index"), error)
                continue
            for finding in draft.findings:
                candidates.append(
                    await self._llm_candidate(
                        finding, batch, segments, full_text, valid_case_ids=valid_case_ids
                    )
                )

        if stats["failed"]:
            logger.warning(
                "本批审查有 %s/%s 批模型输出无效，已按批次降级但不阻塞其余结论",
                stats["failed"],
                stats["calls"],
            )
        return candidates, stats

    async def _retrieve_for_batches(
        self,
        batches: list[list[dict]],
        *,
        contract: Contract,
        focus_rules: list[RedLineRule],
        basis_refs: list[tuple[str, str | None]],
        top_k: int | None,
    ) -> tuple[dict[str, RetrievedProvision], dict[str, RetrievedCase], list[str], list[dict]]:
        """把逐批检索并发出去。

        串行 for 循环里每批要等 embedding HTTP + ES HTTP + 重排 HTTP，
        这些都不依赖上一批的结果，排队做就是白付 N 倍延时。
        并发度沿用 `LLM_MAX_CONCURRENCY`，避免一次审查同时打满连接池与供应商限速。

        第四个返回值是逐批检索过程留痕（融合前各通道名次、精排分、是否进了最终列表）。
        注意 `inPrompt` 的口径是"进了检索器最终列表"，不等于"进了该批提示词"——
        后者还要经 `_rank_provisions` 二次排序与截断，那份名单记在
        `review_llm_calls.provisions_in_prompt`，两个口径分别算 RET-01 与 GEN-08。
        """
        pool: dict[str, RetrievedProvision] = {}
        case_pool: dict[str, RetrievedCase] = {}
        channels: list[str] = []
        events: list[dict] = []
        if not batches:
            return pool, case_pool, channels, events

        semaphore = asyncio.Semaphore(max(1, self._settings.llm_max_concurrency))
        factory = self._side_factory

        async def search_one(batch: list[dict]):
            query = _retrieval_query(batch, contract.contract_kind, focus_rules)
            # AsyncSession 不是并发安全的，共用一个会直接报错，因此每批一个侧会话
            async with semaphore, factory() as side_session:
                retriever = ProvisionRetriever(
                    side_session,
                    self._embedder,
                    self._settings,
                    sparse=self._sparse,
                    reranker=self._reranker,
                )
                items = await retriever.search(query, refs=basis_refs, top_k=top_k)
                cases = await retriever.search_cases(query)
                return items, cases, list(retriever.channels_used), list(retriever.last_diagnostics)

        results = await asyncio.gather(
            *(search_one(batch) for batch in batches), return_exceptions=True
        )

        for index, result in enumerate(results, start=1):
            if isinstance(result, BaseException):
                # 一批检索失败不带走整次审查：规则通道仍在，其余批次照常出结论
                self._degrade("retrieval_batch_failed", f"第 {index} 批检索失败：{result}")
                logger.warning("第 %s 批检索失败，本批仅用已有条文池：%s", index, result)
                continue
            items, cases, used, diagnostics = result
            for item in items:
                pool.setdefault(item.provision_id, item)
            for case in cases:
                case_pool.setdefault(case.case_id, case)
            for kind in used:
                if kind not in channels:
                    channels.append(kind)
            for event in diagnostics:
                event["batchNo"] = index
                events.append(event)

        missing = [channel for channel in ("vector", "bm25", "rerank") if channel not in channels]
        if missing:
            self._degrade(
                "retrieval_channels_degraded",
                f"未参与的召回通道：{', '.join(missing)}（报告不得称完整混合检索）",
            )
        return pool, case_pool, channels, events

    async def _llm_candidate(
        self,
        finding: ReviewFindingDraft,
        batch: list[dict],
        segments: list[dict],
        full_text: str,
        *,
        valid_case_ids: set[str] | None = None,
    ) -> CandidateFinding:
        # 模型回指的 excerpt 来自被转义过的提示词，先换回半角再定位，否则回连不上原文
        excerpt = restore_untrusted(finding.excerpt)
        position = locate_excerpt(segments, full_text, excerpt)
        method = (position or {}).get("method")
        if position is None and finding.segment_index is not None:
            # 退一步：至少归到模型指认的条款上，但标记为未精确定位
            position = _segment_position(segments, finding.segment_index)
            method = "segment_fallback"
        if position is None:
            self._degrade(
                "llm_excerpt_not_located",
                f"模型引用的片段无法回连原文（rule={finding.rule_code or '-'}）：{excerpt[:60]}",
            )

        citations, _ = await resolve_citations(
            self._session,
            [item.model_dump(mode="json", by_alias=True) for item in finding.citations],
        )
        # 判例 id 只认本轮检索给出的，与引用的处理口径一致：编造的 id 直接丢
        case_ids = [
            case_id
            for case_id in (finding.similar_case_ids or [])
            if valid_case_ids is None or case_id in valid_case_ids
        ]

        return CandidateFinding(
            title=finding.title,
            risk_level=finding.risk_level.value,
            clause_category=finding.clause_category.value,
            analysis=finding.analysis,
            suggestion=finding.suggestion,
            rewritten_text=finding.rewritten_text,
            excerpt=excerpt,
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
            similar_case_ids=case_ids,
            located=position is not None,
            locate_method=method or ("segment_fallback" if position else "failed"),
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
                similar_case_ids=item.similar_case_ids,
                judge_verdict=item.judge_verdict,
                human_review_status=item.human_review_status,
                hard_block=item.hard_block,
                verification_status=citation_verification_status(item.citations),
                locate_method=locate_method_name(
                    located=item.located,
                    method=item.locate_method,
                    has_evidence=bool(item.metric_snapshot),
                ),
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
        # 快照补写：JSON 列必须整体重新赋值 SQLAlchemy 才当变化，原地改不会进 UPDATE
        thresholds: Thresholds | None = outcome.get("thresholds")
        review.run_options = {
            **(review.run_options or {}),
            "retrievedCases": [item.to_snapshot() for item in outcome.get("cases") or []],
            "injectionSignals": (outcome.get("injection_hits") or [])[:5],
            "thresholdSnapshot": thresholds.rows() if thresholds else [],
        }
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
        # ---------- 评测埋点（审查级聚合）----------
        review.stage_timings = self._timer.as_dict()
        review.token_usage = summarize_token_usage(self._llm_records)
        review.degradations = self._degradations.to_list()
        review.run_options = {
            **(review.run_options or {}),
            "llmFailures": outcome.get("batch_failures") or [],
            "degradationCodes": self._degradations.codes(),
        }
        contract.status = ContractStatus.REVIEWED.value


    async def _persist_telemetry(self, review_id: uuid.UUID) -> None:
        """把逐次模型调用与检索过程写到旁路会话的独立事务里。

        刻意不与主审查共用事务：一次埋点 INSERT 失败会 abort 整个事务，
        那就等于让"观测"有能力把一份已完成的审查打成失败——本末倒置。
        埋点丢失只该记一条 warning，评测跑分时按缺数据显式报"未验证"。
        """
        if not self._settings.eval_telemetry_enabled:
            return
        if not self._llm_records and not self._retrieval_events:
            return

        store_raw = self._settings.eval_store_raw_output
        try:
            async with self._side_factory() as side_session:
                for record in self._llm_records:
                    side_session.add(
                        ReviewLlmCall(
                            review_id=review_id,
                            slot=str(record.get("slot") or ModelSlot.REVIEW.value),
                            batch_no=int(record.get("batchNo") or 0),
                            model=str(record.get("model") or ""),
                            provider=str(record.get("provider") or ""),
                            reasoning_effort=str(record.get("reasoningEffort") or "default"),
                            prompt_version=str(record.get("promptVersion") or ""),
                            ok=bool(record.get("ok", False)),
                            error_type=str(record.get("errorType") or ""),
                            error_message=str(record.get("errorMessage") or ""),
                            latency_seconds=float(record.get("latencySeconds") or 0.0),
                            prompt_tokens=int(record.get("promptTokens") or 0),
                            completion_tokens=int(record.get("completionTokens") or 0),
                            total_tokens=int(record.get("totalTokens") or 0),
                            reasoning_tokens=int(record.get("reasoningTokens") or 0),
                            finish_reason=str(record.get("finishReason") or ""),
                            chars_in=int(record.get("charsIn") or 0),
                            chars_out=int(record.get("charsOut") or 0),
                            finding_count=int(record.get("findingCount") or 0),
                            provisions_in_prompt=list(record.get("provisionsInPrompt") or []),
                            raw_content=str(record.get("rawContent") or "") if store_raw else "",
                        )
                    )
                for event in self._retrieval_events:
                    side_session.add(
                        ReviewRetrievalEvent(
                            review_id=review_id,
                            batch_no=int(event.get("batchNo") or 0),
                            query=str(event.get("query") or ""),
                            provision_id=str(event.get("provisionId") or ""),
                            law_name=str(event.get("lawName") or ""),
                            article_no=str(event.get("articleNo") or ""),
                            doc_type=str(event.get("docType") or ""),
                            is_sample=bool(event.get("isSample", False)),
                            rank_by_channel=dict(event.get("rankByChannel") or {}),
                            match_kinds=list(event.get("matchKinds") or []),
                            fused_score=float(event.get("fusedScore") or 0.0),
                            rerank_score=event.get("rerankScore"),
                            in_prompt=bool(event.get("inPrompt", False)),
                            dropped_by_rerank=bool(event.get("droppedByRerank", False)),
                            lexical_overlap=float(event.get("lexicalOverlap") or 0.0),
                            latency_seconds=float(event.get("latencySeconds") or 0.0),
                        )
                    )
                await side_session.commit()
        except Exception as exc:  # pragma: no cover - 埋点失败不影响业务
            logger.warning("评测埋点写入失败（不影响审查结果）review=%s：%s", review_id, exc)


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


def _batch_text(batch: list[dict]) -> str:
    """本轮条款的可匹配文本（标题 + 正文前若干字），供词面重叠计算。"""
    return " ".join(
        ((segment.get("title") or "") + " " + (segment.get("text") or "")[:200])
        for segment in batch
    )


def _rank_provisions(
    items: list[RetrievedProvision],
    query_tokens: set[str],
    token_index: dict[str, set[str]],
) -> list[RetrievedProvision]:
    """把与本轮条款更相关的条文排在前面，避免占位记录挤占提示词窗口。

    词面信号用 jieba 词集的重叠率，不再逐字符比对法规名：
    “民法”与“民法典”在字符集合里会重复计分，分词后才是同一个词。
    """

    def score(item: RetrievedProvision) -> tuple:
        return (
            0 if item.is_sample else 1,
            item.score + 0.1 * lexical_overlap(item, query_tokens, token_index),
        )

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
    elif review.judge_model == RULE_CHECK_MODEL:
        parts.append(
            f"复核模型与主审同源，本次改用确定性规则复核（{RULE_CHECK_MODEL}）："
            f"已裁定 {counts.get('judged', 0)} 条，其中 {counts.get('disputed', 0)} 条依据不足或双通道冲突。"
        )
    else:
        independent = (review.run_options or {}).get("judgeIndependent", True)
        judge_line = (
            f"复核模型 {review.judge_model} 已复核 {counts.get('judged', 0)} 条，"
            f"其中 {counts.get('disputed', 0)} 条被判为不成立或把握不足。"
        )
        if not independent:
            judge_line += "注意：它与主审同源，不构成独立信号。"
        if review.judge_failed_count:
            judge_line += f" 另有 {review.judge_failed_count} 批复核调用失败，相关结论标记为未复核。"
        parts.append(judge_line)

    if outcome.get("injection_hits"):
        parts.append(
            f"合同正文含 {len(outcome['injection_hits'])} 处疑似提示注入措辞，"
            "已隔离为数据处理并记录，需人工确认是否为故意规避。"
        )

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
