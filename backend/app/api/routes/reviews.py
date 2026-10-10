"""审查接口：发起、查询历史、拉取报告。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db
from app.api.mappers import to_finding_out, to_review_summary
from app.models.contract import Contract
from app.models.enums import ContractStatus, ReviewStatus
from app.models.review import ContractReview
from app.schemas.common import PageMeta
from app.schemas.review import (
    CitationOut,
    ReviewListResponse,
    ReviewReport,
    ReviewRunRequest,
    ReviewSummary,
)
from app.tasks.review_runner import run_review_task

router = APIRouter(tags=["reviews"])

_UNREVIEWABLE = {
    ContractStatus.UPLOADED.value,
    ContractStatus.PARSING.value,
    ContractStatus.FAILED.value,
}


@router.post(
    "/contracts/{contract_id}/reviews",
    response_model=ReviewSummary,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_review(
    contract_id: uuid.UUID,
    background: BackgroundTasks,
    payload: ReviewRunRequest | None = None,
    session: AsyncSession = Depends(get_db),
) -> ReviewSummary:
    """发起一次审查，立即返回任务凭据，执行放到后台。

    历史不覆盖：每轮审查都新增一条 `ContractReview`，合规场景需要"当时怎么判的"可追溯。
    """
    contract = await session.get(Contract, contract_id)
    if contract is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="合同不存在")
    if contract.status in _UNREVIEWABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"合同当前状态为 {contract.status}，需先完成解析才能审查",
        )

    request = payload or ReviewRunRequest()
    review = ContractReview(
        contract_id=contract.id,
        status=ReviewStatus.PENDING.value,
        run_options=request.model_dump(mode="json", by_alias=True),
    )
    session.add(review)
    await session.commit()

    background.add_task(run_review_task, review.id, request)
    return to_review_summary(review)


@router.get("/contracts/{contract_id}/reviews", response_model=ReviewListResponse)
async def list_contract_reviews(
    contract_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
    session: AsyncSession = Depends(get_db),
) -> ReviewListResponse:
    stmt = (
        select(ContractReview)
        .where(ContractReview.contract_id == contract_id)
        .order_by(ContractReview.created_at.desc())
    )
    rows = await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    items = [to_review_summary(review) for review in rows.scalars().all()]
    return ReviewListResponse(
        items=items,
        meta=PageMeta(total=len(items), page=page, page_size=page_size),
    )


@router.get("/reviews/{review_id}", response_model=ReviewSummary)
async def get_review(
    review_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> ReviewSummary:
    review = await session.get(ContractReview, review_id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="审查任务不存在")
    return to_review_summary(review)


@router.get("/reviews/{review_id}/report", response_model=ReviewReport)
async def get_review_report(
    review_id: uuid.UUID, session: AsyncSession = Depends(get_db)
) -> ReviewReport:
    """完整报告：结论 + 溯源引用 + 参考条文 + 指标告警。

    前端 PDF.js 高亮所需的 `char_start/char_end/page_no` 全部来自这里，
    因此未定位成功的结论会以 `char_start=None` 原样返回，由界面提示"无法定位原文"。
    """
    stmt = (
        select(ContractReview)
        .where(ContractReview.id == review_id)
        .options(selectinload(ContractReview.findings), selectinload(ContractReview.contract))
    )
    review = (await session.execute(stmt)).scalars().first()
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="审查任务不存在")

    findings = sorted(review.findings or [], key=lambda item: item.order_index)
    contract: Contract = review.contract

    return ReviewReport(
        review=to_review_summary(review),
        findings=[to_finding_out(finding) for finding in findings],
        reference_provisions=[
            CitationOut.model_validate(item) for item in review.retrieved_provisions or []
        ],
        metric_warnings=list((contract.extracted_metrics or {}).get("warnings", [])),
    )
