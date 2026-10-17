"""结论复核（judge）的调用编排。

分工刻意切开：`findings.apply_judge_verdicts` 是不碰网络的纯函数（怎么写回、
什么时候加严），本模块只负责"把结论送进复核模型、把返回解析成 {序号: 裁定}"。
复核用的模型与主审查模型分属两套配置，同一家同一个模型给自己盖章没有独立价值。

主审与复核同模型时（回音壁）不是只有“照样叫模型”和“不复核”两个选项：
`run_rule_check_judge` 用确定性信号（引用是否已核验、双通道是否冲突、
能否回连原文）自己出一条裁定，至少不会像同模型复核那样只会附和。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.enums import CrossCheckStatus, FindingSource, ModelSlot
from app.schemas.review import JudgeBatchDraft
from app.services.findings import CandidateFinding
from app.services.llm.base import LLMError, LLMProvider
from app.services.llm.factory import JUDGE_MODE_RULE_CHECK, RULE_CHECK_MODEL, JudgeSlot
from app.services.llm.prompts import PROMPT_VERSION, build_judge_system_prompt, build_judge_user_prompt
from app.services.rag.retriever import RetrievedProvision

logger = get_logger(__name__)


@dataclass(slots=True)
class JudgeOutcome:
    """一次复核运行的结果。`model` 为空串表示根本没调模型。"""

    model: str = ""
    calls: int = 0
    failed_batches: int = 0
    verdicts: dict[int, dict] = field(default_factory=dict)
    #: 逐次调用留痕（token、思考 token、延时、失败原因），写 `review_llm_calls`。
    #: 复核槽位现在是独立供应商（DeepSeek），成本与失败率必须与主审分开计
    calls_detail: list[dict] = field(default_factory=list)

    @property
    def executed(self) -> bool:
        return self.calls > 0


def _case_payload(index: int, item: CandidateFinding) -> dict:
    return {
        "index": index,
        "title": item.title,
        "riskLevel": item.risk_level,
        "clauseCategory": item.clause_category,
        "source": item.source,
        "excerpt": item.excerpt,
        "analysis": item.analysis,
        "suggestion": item.suggestion,
        "confidence": round(item.confidence, 4),
        "citations": item.citations,
    }


def _case_size(case: dict) -> int:
    return sum(len(str(case.get(key) or "")) for key in ("title", "excerpt", "analysis", "suggestion"))


def _plan_batches(
    items: list[CandidateFinding], *, max_items: int, char_budget: int
) -> list[list[tuple[int, dict]]]:
    """按条数与字符双重上限切批，返回 (全局序号, case) 的分批结果。

    用全局序号而不是批内序号：裁定要能对上原结论列表，
    否则一批重排就会把质疑错记到别的结论上。
    """
    limit = max(1, max_items)
    batches: list[list[tuple[int, dict]]] = []
    current: list[tuple[int, dict]] = []
    budget = 0

    for index, item in enumerate(items):
        case = _case_payload(index, item)
        size = _case_size(case)
        if current and (len(current) >= limit or budget + size > char_budget):
            batches.append(current)
            current, budget = [], 0
        current.append((index, case))
        budget += size

    if current:
        batches.append(current)
    return batches


async def run_judge(
    slot: JudgeSlot,
    items: list[CandidateFinding],
    provisions: list[RetrievedProvision],
    app_settings: Settings,
) -> JudgeOutcome:
    """复核入口：按复核槽位的形态选模型复核或确定性规则复核。"""
    if slot.mode == JUDGE_MODE_RULE_CHECK:
        return run_rule_check_judge(items)
    return await run_llm_judge(slot.provider, items, provisions, app_settings)


async def run_llm_judge(
    provider: LLMProvider | None,
    items: list[CandidateFinding],
    provisions: list[RetrievedProvision],
    app_settings: Settings,
) -> JudgeOutcome:
    """逐批复核结论。单批失败只影响该批，其余裁定照常回收。

    失败批次计入 `failed_batches`，对应结论在写回阶段标记为"未复核"，
    不会被当成"复核通过"。
    """
    if provider is None or not items:
        return JudgeOutcome()

    batches = _plan_batches(
        items,
        max_items=app_settings.judge_max_items_per_call,
        char_budget=app_settings.judge_char_budget_per_call,
    )
    outcome = JudgeOutcome(model=provider.model)
    semaphore = asyncio.Semaphore(max(1, app_settings.judge_max_concurrency))
    prompt_provisions = provisions[: app_settings.rag_top_k * 2]
    system_prompt = build_judge_system_prompt(prompt_provisions)
    #: 本批复核看了哪些条文：与主审同口径留痕，才能算"给了依据却没引用"（GEN-08 分母）
    prompt_provision_ids = [item.provision_id for item in prompt_provisions]

    async def invoke(
        batch_no: int, cases: list[dict]
    ) -> tuple[int, list[dict], Exception | None, dict]:
        user_prompt = build_judge_user_prompt(
            cases,
            batch_no=batch_no,
            batch_total=len(batches),
            isolate=app_settings.prompt_isolate_untrusted,
        )
        record = {
            "slot": ModelSlot.JUDGE.value,
            "batchNo": batch_no,
            "model": provider.model,
            "provider": provider.name,
            "reasoningEffort": getattr(provider, "reasoning_effort", "") or "default",
            "promptVersion": PROMPT_VERSION,
            "charsIn": len(system_prompt) + len(user_prompt),
            "provisionsInPrompt": prompt_provision_ids,
            "ok": False,
        }
        started = time.perf_counter()
        async with semaphore:
            try:
                result = await provider.complete_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
                draft = JudgeBatchDraft.model_validate(result.payload)
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
                        "findingCount": len(draft.verdicts),
                        "rawContent": result.raw_content or "",
                    }
                )
                verdicts = [verdict.model_dump(mode="json") for verdict in draft.verdicts]
                return 0, verdicts, None, record
            except (LLMError, ValidationError) as exc:
                record.update(
                    {
                        "latencySeconds": round(time.perf_counter() - started, 3),
                        "errorType": type(exc).__name__,
                        "errorMessage": str(exc)[:1000],
                    }
                )
                return 1, [], exc, record

    results = await asyncio.gather(
        *(invoke(no, [case for _, case in batch]) for no, batch in enumerate(batches, 1))
    )

    verdict_payloads: list[dict]
    for batch_no, (batch, result) in enumerate(zip(batches, results, strict=True), start=1):
        failed, verdict_payloads, error, record = result
        outcome.calls += 1
        outcome.calls_detail.append(record)
        if failed:
            outcome.failed_batches += 1
            logger.warning("复核批次 %s 失败：%s", batch_no, error)
            continue

        # 序号映射按"该批实际送出去的 index"回填，模型编造的序号直接丢弃
        valid_indices = {index for index, _ in batch}
        for verdict in verdict_payloads:
            index = verdict.get("index")
            if index not in valid_indices:
                logger.warning("复核返回了未知序号 %s，已丢弃", index)
                continue
            outcome.verdicts[int(index)] = verdict

    logger.info(
        "结论复核完成：模型=%s 批次=%s 失败=%s 取回裁定=%s",
        outcome.model,
        len(batches),
        outcome.failed_batches,
        len(outcome.verdicts),
    )
    return outcome


def _verified_citations(item: CandidateFinding) -> list[dict]:
    """已在知识库命中且非占位的引用——“写了个《法规名》”不等于依据成立。"""
    return [
        citation
        for citation in item.citations
        if citation.get("verified", False)
        and not citation.get("isSample")
        and not citation.get("is_sample")
    ]


def rule_check_verdict(index: int, item: CandidateFinding) -> dict:
    """不开模型也能给的复核裁定，只回答“这条结论站不站得住”。

    口径与提示词复核一致（只加严不减严）：它不会改等级，
    只会把“拿不出已核验依据 / 双通道冲突 / 回连不上原文”的结论标为不成立，
    后续 `apply_judge_verdicts` 自然把它们推进人工队列。
    """
    verified = _verified_citations(item)

    if not item.citations:
        agree, confidence, reason = False, 0.90, "结论未给出任何法规依据，无法确认其成立性"
    elif not verified:
        agree, confidence, reason = (
            False,
            0.90,
            "所引条文均未在知识库核验（占位登记或原文未导入），依据不支撑结论",
        )
    elif item.cross_check_status == CrossCheckStatus.RULE_OVERRIDES.value:
        agree, confidence, reason = (
            False,
            0.85,
            "双通道定性冲突，虽以规则引擎为准，但分歧本身需人工确认",
        )
    elif item.source == FindingSource.LLM.value and not item.located:
        agree, confidence, reason = (
            False,
            0.80,
            "仅模型单通道判定且未能回连合同原文，无法验证引用片段真实性",
        )
    else:
        agree = True
        # 规则硬命中的可复现，把握度高；纯模型结论即使引用已核验也不给满分
        confidence = 0.90 if item.rule_confidence >= 0.9 else 0.70
        reason = "引用已核验" + ("且规则硬命中" if item.rule_confidence >= 0.9 else "，但仅模型定性")

    return {
        "index": index,
        "agree": agree,
        "citation_ok": bool(verified),
        "suggested_risk_level": item.risk_level,
        "confidence": confidence,
        "reason": reason,
    }


def run_rule_check_judge(items: list[CandidateFinding]) -> JudgeOutcome:
    """确定性规则复核：主审与复核同模型时的降级口径（`JUDGE_SAME_MODEL_POLICY=rule_check`）。

    它不是“没复核”：裁定依据全部可复现，且不碰网络因此不会超时；
    代价是只会用硬信号判属性一致性，读不懂“这个定性在法律上对不对”。
    """
    if not items:
        return JudgeOutcome()
    verdicts = {index: rule_check_verdict(index, item) for index, item in enumerate(items)}
    outcome = JudgeOutcome(model=RULE_CHECK_MODEL, calls=1, verdicts=verdicts)
    #: 降级口径也要留痕：否则"复核过 12 条"在评测里看不出是模型复核还是确定性复核
    outcome.calls_detail.append(
        {
            "slot": ModelSlot.JUDGE.value,
            "batchNo": 1,
            "model": RULE_CHECK_MODEL,
            "provider": "deterministic",
            "reasoningEffort": "none",
            "promptVersion": PROMPT_VERSION,
            "ok": True,
            "latencySeconds": 0.0,
            "charsIn": 0,
            "charsOut": 0,
            "findingCount": len(verdicts),
            "disputedCount": sum(1 for verdict in verdicts.values() if not verdict["agree"]),
            "provisionsInPrompt": [],
        }
    )
    logger.info(
        "确定性规则复核：结论 %s 条，判为不成立 %s 条（未调模型）",
        len(items),
        sum(1 for verdict in verdicts.values() if not verdict["agree"]),
    )
    return outcome
