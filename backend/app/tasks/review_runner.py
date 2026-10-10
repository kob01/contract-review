"""审查任务的后台执行。

FastAPI `BackgroundTasks` 在响应返回后执行，因此必须自带会话边界；
把请求级 session 传进后台任务是这类接口最常见的隐性 bug 来源。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models.contract import Contract
from app.models.enums import ContractStatus, ReviewStatus
from app.models.review import ContractReview
from app.schemas.review import ReviewRunRequest
from app.services.llm.factory import build_provider
from app.services.rag.embeddings import EmbeddingNotConfigured, build_embedding_client
from app.services.review_pipeline import ReviewPipeline

logger = get_logger(__name__)


async def run_review_task(review_id: uuid.UUID, request: ReviewRunRequest) -> None:
    """执行一次审查。异常已在内部消化并写回状态，不让后台任务静默崩溃。"""
    settings = get_settings()

    try:
        provider = build_provider(settings)
        embedder = build_embedding_client(settings)
    except EmbeddingNotConfigured as exc:
        logger.error("向量化配置缺失，审查中止：%s", exc)
        await _mark_failed(review_id, f"配置缺失：{exc}")
        return

    try:
        async with session_scope() as session:
            pipeline = ReviewPipeline(session, provider, embedder, settings)
            await pipeline.run(review_id, request)
    except Exception as exc:  # pragma: no cover - 取决于模型与数据库状态
        logger.exception("审查执行失败 review=%s：%s", review_id, exc)
        # 管道已提交 failed 状态时这里不会覆盖成功结果，只兜住管道之外的异常
        await _mark_failed(review_id, f"{type(exc).__name__}: {exc}")


async def _mark_failed(review_id: uuid.UUID, message: str) -> None:
    try:
        async with session_scope() as session:
            review = await session.get(ContractReview, review_id)
            if review is None:
                return
            review.status = ReviewStatus.FAILED.value
            review.error_message = message[:2000]
            review.finished_at = datetime.now(UTC)
            # 异步会话不能靠懒加载取 contract，否则触发 MissingGreenlet
            contract = await session.get(Contract, review.contract_id)
            if contract is not None:
                contract.status = ContractStatus.FAILED.value
    except Exception as exc:  # pragma: no cover - 兜底写状态本身失败
        logger.error("写入失败状态时出错 review=%s：%s", review_id, exc)
