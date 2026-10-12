"""僵尸审查巡检。

状态机里最容易漏的一笔是"没人负责收尾"：worker 被 OOM 杀掉、容器被驱逐、
主机重启时，它自己的 except 分支根本不会执行，库里的审查就永远停在 running，
合同永远停在 reviewing，前端只能一直转圈。

巡检补上这个兜底：超过阈值仍没有心跳的任务强制置为 failed，并把合同状态从
reviewing 放出来，用户能看到明确的失败原因并重新发起审查。

阈值口径由 `effective_zombie_timeout_seconds` 给出（不小于队列硬超时），
否则"跑得慢"会被误判成"已经死"，把好端端的任务砍成失败。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models.contract import Contract
from app.models.enums import ContractStatus, ReviewStatus
from app.models.review import ContractReview
from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

logger = get_logger(__name__)

#: 单次巡检最多处理多少条：巡检是兜底不是主链路，不该因为它锁住太久
_BATCH_LIMIT = 200


def _stale_condition(cutoff: datetime) -> ColumnElement:
    """心跳缺失即回退到行更新时间：入队后从未被受理的任务同样要能被发现。"""
    return func.coalesce(ContractReview.heartbeat_at, ContractReview.updated_at) < cutoff


def zombie_reason(status: str, timeout_seconds: int) -> str:
    """给用户的失败原因：“没人受理”与“跑一半死了”处理方式不同，不能写同一句话。"""
    minutes = max(1, timeout_seconds // 60)
    if status == ReviewStatus.PENDING.value:
        return (
            f"任务超过 {minutes} 分钟未被受理，已由巡检判定为失败："
            "worker 未运行或队列不可达，请检查 worker 进程与 Redis。"
        )
    return (
        f"任务超过 {minutes} 分钟无心跳，已由巡检判定为失败："
        "执行进程可能已崩溃（OOM/驱逐/发版），可重新发起审查。"
    )


async def reap_zombie_reviews(ctx: dict | None = None) -> int:
    """把超时无心跳的 pending/running 审查强制置为 failed，返回处理条数。

    作为 ARQ cron 任务注册在 worker 里，也在 API 进程内循环调用（降级模式）。
    """
    del ctx  # ARQ cron 传入的运行上下文
    settings = get_settings()
    if not settings.zombie_watchdog_enabled:
        return 0

    timeout = settings.effective_zombie_timeout_seconds
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout)

    async with session_scope() as session:
        stmt = (
            select(ContractReview)
            .where(
                ContractReview.status.in_(
                    [ReviewStatus.PENDING.value, ReviewStatus.RUNNING.value]
                )
            )
            .where(_stale_condition(cutoff))
            .limit(_BATCH_LIMIT)
        )
        candidates = list((await session.execute(stmt)).scalars().all())
        if not candidates:
            return 0

        # 队列还开着时，“尚未执行”不等于“已死”：worker 忙不过来时任务会在队列里排队，
        # 把它们置失败会把正常积压的单子砍掉（延迟导入避开与 queue 的环依赖）
        from app.tasks.queue import job_is_alive

        reviews: list[ContractReview] = []
        skipped = 0
        for review in candidates:
            if review.status == ReviewStatus.PENDING.value and await job_is_alive(
                review.id, settings
            ):
                skipped += 1
                continue
            reviews.append(review)
        if not reviews:
            logger.info("僵尸巡检：%s 条超时长任务仍在队列中排队，本轮不动", skipped)
            return 0

        now = datetime.now(UTC)
        for review in reviews:
            never_started = review.status == ReviewStatus.PENDING.value
            review.status = ReviewStatus.FAILED.value
            review.error_message = zombie_reason(
                "pending" if never_started else ReviewStatus.RUNNING.value, timeout
            )
            review.finished_at = now
            review.heartbeat_at = now

            contract = await session.get(Contract, review.contract_id)
            # 只回滚"审查中"，别把已解析/已审查的状态改成失败
            if contract is not None and contract.status == ContractStatus.REVIEWING.value:
                contract.status = ContractStatus.FAILED.value

            logger.warning(
                "巡检重置僵尸审查 review=%s contract=%s 原状态=%s 最后心跳=%s",
                review.id,
                review.contract_id,
                "pending" if never_started else "running",
                review.started_at.isoformat() if review.started_at else "—",
            )

    logger.info(
        "僵尸巡检完成：重置 %s 条（阈值 %ss，另有 %s 条仍在队列中）",
        len(reviews),
        timeout,
        skipped,
    )
    return len(reviews)


async def watch_zombie_reviews_loop() -> None:
    """进程内巡检循环。

    `QUEUE_ENABLED=false`（或被 Redis 拒绝而退回进程内执行）时没有 worker 跑 cron，
    巡检就必须在 API 进程里自己转起来，否则降级模式下状态机彻底失去兜底。
    """
    settings = get_settings()
    interval = max(30, settings.zombie_watchdog_interval_seconds)
    logger.info("僵尸巡检循环已启动：每 %ss 一次", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await reap_zombie_reviews()
            except Exception as exc:  # pragma: no cover - 取决于数据库状态
                logger.warning("僵尸巡检失败，下个周期重试：%s", exc)
    except asyncio.CancelledError:
        logger.info("僵尸巡检循环已停止")
        raise
