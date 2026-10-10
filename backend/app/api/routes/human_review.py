"""人工法务复核接口（Human-in-the-loop）。

复核不是"给人看一遍"就完事：每次确认/驳回/调级都写入 `HumanReviewTask`，
并可导出为标注集。误报率、专家调级分布是模型迭代唯一可靠的反馈来源。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.mappers import to_task_out
from app.models.contract import Contract
from app.models.enums import HumanReviewStatus, RiskLevel
from app.models.human_review import HumanReviewTask
from app.models.review import ContractReview, ReviewFinding
from app.schemas.common import PageMeta
from app.schemas.review import (
    HumanReviewQueueResponse,
    HumanReviewResolveRequest,
    HumanReviewTaskOut,
)

router = APIRouter(prefix="/human-review", tags=["human-review"])

_VALID_VERDICTS = {"confirmed", "false_positive", "need_adjustment"}


@router.get("/queue", response_model=HumanReviewQueueResponse)
async def list_queue(
    status_filter: HumanReviewStatus | None = Query(default=None, alias="status"),
    contract_id: uuid.UUID | None = None,
    review_id: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
) -> HumanReviewQueueResponse:
    stmt = select(HumanReviewTask)
    return await _query_queue(stmt, session, status_filter, contract_id, review_id, page, page_size)


async def _query_queue(
    stmt, session, status_filter, contract_id, review_id, page, page_size
) -> HumanReviewQueueResponse:
    if status_filter is not None:
        stmt = stmt.where(HumanReviewTask.status == status_filter.value)
    if contract_id is not None:
        stmt = stmt.where(HumanReviewTask.contract_id == contract_id)
    if review_id is not None:
        stmt = stmt.where(HumanReviewTask.review_id == review_id)

    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    rows = await session.execute(
        stmt.order_by(HumanReviewTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    items: list[HumanReviewTaskOut] = []
    for task in rows.scalars().all():
        finding = await session.get(ReviewFinding, task.finding_id)
        items.append(to_task_out(task, finding=finding))

    return HumanReviewQueueResponse(
        items=items,
        meta=PageMeta(total=int(total or 0), page=page, page_size=page_size),
    )


@router.post("/tasks/{task_id}/resolve", response_model=HumanReviewTaskOut)
async def resolve_task(
    task_id: uuid.UUID,
    payload: HumanReviewResolveRequest,
    session: AsyncSession = Depends(get_db),
) -> HumanReviewTaskOut:
    """提交复核结论，同步回写结论本身的状态与调级结果。"""
    if payload.verdict not in _VALID_VERDICTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"verdict 只能是 {sorted(_VALID_VERDICTS)}",
        )

    task = await session.get(HumanReviewTask, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="复核任务不存在")

    finding = await session.get(ReviewFinding, task.finding_id)
    if finding is None:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="原结论已被删除")

    task.status = {
        "confirmed": HumanReviewStatus.APPROVED.value,
        "false_positive": HumanReviewStatus.REJECTED.value,
        "need_adjustment": HumanReviewStatus.ADJUSTED.value,
    }[payload.verdict]
    task.assignee = payload.reviewer_id
    task.comment = payload.comment
    task.expert_verdict = payload.verdict
    task.expert_risk_level = (
        payload.adjusted_risk_level.value if payload.adjusted_risk_level else None
    )
    task.resolved_at = datetime.now(UTC)

    finding.human_review_status = task.status
    finding.human_reviewer_id = payload.reviewer_id
    finding.human_comment = payload.comment
    finding.adjusted_risk_level = task.expert_risk_level
    # 专家判为误报时直接下调风险等级，报告页随之收敛
    if payload.verdict == "false_positive":
        finding.risk_level = RiskLevel.NONE.value
    elif payload.verdict == "need_adjustment" and payload.adjusted_risk_level:
        finding.risk_level = payload.adjusted_risk_level.value

    review = await session.get(ContractReview, task.review_id)
    if review is not None:
        pending = await session.scalar(
            select(func.count())
            .select_from(HumanReviewTask)
            .where(
                HumanReviewTask.review_id == review.id,
                HumanReviewTask.status == HumanReviewStatus.PENDING.value,
            )
        )
        review.pending_human_count = int(pending or 0)
        review.needs_human_review = review.pending_human_count > 0

    await session.commit()
    return to_task_out(task, finding=finding)


@router.get("/stats")
async def review_stats(session: AsyncSession = Depends(get_db)) -> dict:
    """队列水位与误报率，用于判断置信度阈值是否需要调整。"""
    pending = await session.scalar(
        select(func.count())
        .select_from(HumanReviewTask)
        .where(HumanReviewTask.status == HumanReviewStatus.PENDING.value)
    )
    resolved = await session.scalar(
        select(func.count())
        .select_from(HumanReviewTask)
        .where(HumanReviewTask.status != HumanReviewStatus.PENDING.value)
    )
    false_positive = await session.scalar(
        select(func.count())
        .select_from(HumanReviewTask)
        .where(HumanReviewTask.expert_verdict == "false_positive")
    )
    adjusted = await session.scalar(
        select(func.count())
        .select_from(HumanReviewTask)
        .where(HumanReviewTask.expert_verdict == "need_adjustment")
    )

    total_resolved = int(resolved or 0)
    return {
        "pending": int(pending or 0),
        "resolved": total_resolved,
        "false_positive": int(false_positive or 0),
        "adjusted": int(adjusted or 0),
        "false_positive_rate": round(int(false_positive or 0) / total_resolved, 4)
        if total_resolved
        else None,
        "adjustment_rate": round(int(adjusted or 0) / total_resolved, 4) if total_resolved else None,
    }


@router.get("/export/sft")
async def export_annotation_samples(
    limit: int = Query(default=500, ge=1, le=5000),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """导出已复核样本（prompt/label 形式），供后续 SFT 或偏好对齐使用。

    导出内容包含合同片段，属于敏感数据：接口只在内部环境启用，
    且导出即打标记，便于追溯哪批样本已流出。
    """
    rows = await session.execute(
        select(HumanReviewTask)
        .where(
            HumanReviewTask.status != HumanReviewStatus.PENDING.value,
            HumanReviewTask.exported_for_sft.is_(False),
        )
        .order_by(HumanReviewTask.resolved_at.desc())
        .limit(limit)
    )
    tasks = list(rows.scalars().all())

    samples: list[dict] = []
    for task in tasks:
        finding = await session.get(ReviewFinding, task.finding_id)
        contract = await session.get(Contract, task.contract_id)
        samples.append(
            {
                "id": str(task.id),
                "instruction": (
                    "审查以下信贷合同条款是否存在合规问题，并给出风险等级与修改建议。"
                ),
                "input": (finding.excerpt if finding else "") or "",
                "contract_title": contract.title if contract else None,
                "model_output": task.model_snapshot,
                "expert": {
                    "verdict": task.expert_verdict,
                    "risk_level": task.expert_risk_level,
                    "comment": task.comment,
                    "reviewer_id": task.assignee,
                },
            }
        )
        task.exported_for_sft = True

    await session.commit()
    # 逐行 JSON 便于直接喂给训练脚本
    return {
        "count": len(samples),
        "jsonl": "\n".join(json.dumps(sample, ensure_ascii=False) for sample in samples),
    }
