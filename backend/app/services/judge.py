"""结论复核（LLM judge）的调用编排。

分工刻意切开：`findings.apply_judge_verdicts` 是不碰网络的纯函数（怎么写回、
什么时候加严），本模块只负责"把结论送进复核模型、把返回解析成 {序号: 裁定}"。
复核用的模型与主审查模型分属两套配置，同一家同一个模型给自己盖章没有独立价值。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.review import JudgeBatchDraft
from app.services.findings import CandidateFinding
from app.services.llm.base import LLMError, LLMProvider
from app.services.llm.prompts import build_judge_system_prompt, build_judge_user_prompt
from app.services.rag.retriever import RetrievedProvision

logger = get_logger(__name__)

#: 单批复核的字符预算，超过就再切批；让模型只看前半截就下结论比不复核更糟
_CHAR_BUDGET = 9000


@dataclass(slots=True)
class JudgeOutcome:
    """一次复核运行的结果。`model` 为空串表示根本没调模型。"""

    model: str = ""
    calls: int = 0
    failed_batches: int = 0
    verdicts: dict[int, dict] = field(default_factory=dict)

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


def _plan_batches(items: list[CandidateFinding], *, max_items: int) -> list[list[tuple[int, dict]]]:
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
        if current and (len(current) >= limit or budget + size > _CHAR_BUDGET):
            batches.append(current)
            current, budget = [], 0
        current.append((index, case))
        budget += size

    if current:
        batches.append(current)
    return batches


async def run_judge(
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

    batches = _plan_batches(items, max_items=app_settings.judge_max_items_per_call)
    outcome = JudgeOutcome(model=provider.model)
    semaphore = asyncio.Semaphore(max(1, app_settings.judge_max_concurrency))
    system_prompt = build_judge_system_prompt(provisions[: app_settings.rag_top_k * 2])

    async def invoke(batch_no: int, cases: list[dict]) -> tuple[int, list[dict], Exception | None]:
        async with semaphore:
            try:
                result = await provider.complete_json(
                    system_prompt=system_prompt,
                    user_prompt=build_judge_user_prompt(cases, batch_no=batch_no, batch_total=len(batches)),
                )
                draft = JudgeBatchDraft.model_validate(result.payload)
                return 0, [verdict.model_dump(mode="json") for verdict in draft.verdicts], None
            except (LLMError, ValueError) as exc:
                return 1, [], exc

    results = await asyncio.gather(
        *(invoke(no, [case for _, case in batch]) for no, batch in enumerate(batches, 1))
    )

    for batch_no, (batch, result) in enumerate(zip(batches, results, strict=True), start=1):
        failed, verdict_payloads, error = result
        outcome.calls += 1
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
