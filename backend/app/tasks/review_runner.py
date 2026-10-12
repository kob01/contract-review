"""审查任务的执行编排。

这份代码同时服务两种执行环境：ARQ worker 进程（默认）与进程内后台任务
（Redis 不可达时的回退）。两边都必须自带会话边界——把请求级 session 传进
后台任务是这类接口最常见的隐性 bug 来源。

两种失败必须分清：
- **业务失败**（合同未解析、模型输出不合法、配置缺失）：写回 failed 就结束了，
  重试只会再失败一次，因此不向上抛，ARQ 不会重投；
- **基础设施失败**（进程被驱逐、OOM、断电）：没人来得及写状态，库里会永远停在
  running 让前端假等，这部分交给 `app.tasks.watchdog` 按心跳超时强制收尾。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models.contract import Contract
from app.models.enums import ContractStatus, ReviewStatus
from app.models.review import ContractReview
from app.schemas.review import ReviewRunRequest
from app.services.llm.factory import shared_judge_slot, shared_provider
from app.services.rag.embeddings import EmbeddingNotConfigured, shared_embedder
from app.services.rag.rerank import shared_reranker
from app.services.rag.sparse import shared_sparse_index
from app.services.review_pipeline import ReviewPipeline
from sqlalchemy import update

logger = get_logger(__name__)


async def run_review_task(
    review_id: uuid.UUID, request: ReviewRunRequest, *, job_try: int = 1
) -> None:
    """执行一次审查。异常已在内部消化并写回状态，不让后台任务静默崩溃。"""
    settings = get_settings()

    # 客户端一律取进程级单例：worker 长跑复用，不再每轮审查新建连接池
    provider = shared_provider(settings)
    try:
        embedder = shared_embedder(settings)
    except EmbeddingNotConfigured as exc:
        logger.error("向量化配置缺失，审查中止：%s", exc)
        await _mark_failed(review_id, f"配置缺失：{exc}")
        return

    judge_slot = shared_judge_slot(settings)

    # 心跳与审查并行：单轮审查可能十几分钟不写库，没有心跳就只能把
    # “慢任务”和“死任务”混在一起判
    heartbeat = asyncio.create_task(_heartbeat_loop(review_id))
    try:
        async with session_scope() as session:
            pipeline = ReviewPipeline(
                session,
                provider,
                embedder,
                settings,
                sparse=shared_sparse_index(settings),
                reranker=shared_reranker(settings),
                judge_slot=judge_slot,
            )
            await pipeline.run(review_id, request)
    except asyncio.CancelledError:
        # 队列硬超时或进程退出：留给巡检收尾，这里不伪装成业务失败
        logger.warning("审查任务被取消 review=%s（job_try=%s）", review_id, job_try)
        raise
    except Exception as exc:  # pragma: no cover - 取决于模型与数据库状态
        logger.exception("审查执行失败 review=%s：%s", review_id, exc)
        # 管道已提交 failed 状态时这里不会覆盖成功结果，只兜住管道之外的异常
        await _mark_failed(review_id, f"{type(exc).__name__}: {exc}")
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await heartbeat


async def _heartbeat_loop(review_id: uuid.UUID) -> None:
    """周期性刷新 `heartbeat_at`，向巡检证明任务还在推进。"""
    settings = get_settings()
    interval = max(10, settings.heartbeat_interval_seconds)
    stamp = datetime.now(UTC)

    while True:
        await asyncio.sleep(interval)
        try:
            async with session_scope() as session:
                await session.execute(
                    update(ContractReview)
                    .where(ContractReview.id == review_id)
                    .where(
                        ContractReview.status.in_(
                            [ReviewStatus.PENDING.value, ReviewStatus.RUNNING.value]
                        )
                    )
                    .values(heartbeat_at=stamp)
                )
        except Exception as exc:  # pragma: no cover - 取决于数据库状态
            logger.warning("心跳刷新失败 review=%s：%s", review_id, exc)
        stamp = datetime.now(UTC)


async def run_review_job(ctx: dict | None, review_id: str, request: dict | None = None) -> None:
    """ARQ 任务入口：参数必须是可序列化的基础类型，因此收 UUID 字符串与 dict。

    业务失败不向上抛（见模块 docstring），否则一次法律定性错误会被重试成
    三次模型调用与三份互相矛盾的审查记录。
    """
    job_try = int((ctx or {}).get("job_try", 1) or 1)
    await run_review_task(
        uuid.UUID(review_id),
        ReviewRunRequest.model_validate(request or {}),
        job_try=job_try,
    )


async def _mark_failed(review_id: uuid.UUID, message: str) -> None:
    try:
        async with session_scope() as session:
            review = await session.get(ContractReview, review_id)
            if review is None:
                return
            review.status = ReviewStatus.FAILED.value
            review.error_message = message[:2000]
            review.finished_at = datetime.now(UTC)
            review.heartbeat_at = datetime.now(UTC)
            # 异步会话不能靠懒加载取 contract，否则触发 MissingGreenlet
            contract = await session.get(Contract, review.contract_id)
            if contract is not None:
                contract.status = ContractStatus.FAILED.value
    except Exception as exc:  # pragma: no cover - 兜底写状态本身失败
        logger.error("写入失败状态时出错 review=%s：%s", review_id, exc)
