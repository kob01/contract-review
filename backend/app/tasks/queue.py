"""任务队列装配（ARQ + Redis）。

为什么不能继续用 FastAPI `BackgroundTasks`：它在 Web 进程内执行，而一轮审查是
分钟级长任务。发版、OOM、K8s 驱逐都会把正在跑的任务直接带走，数据库里的状态
永远停在 running/reviewing，前端永远等不到回调——这不是"偶尔失败"，而是
"失败后连事实都丢了"。API 进程与 worker 进程分离后，API 重启不影响在跑的任务。

Redis 不可达时的回退在 `enqueue_review`：宁可在 API 进程内把已接下的单子跑完，
也不把它报错丢掉；但必须日志记下降级，否则会长期掩盖"队列其实没起来"。

`WorkerSettings` 供 `arq app.tasks.queue.WorkerSettings` 与 `python -m app.tasks.worker`
两种启动方式使用，字段全部来自配置，不在代码里另立一份口径。
"""

from __future__ import annotations

import asyncio
import uuid

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.schemas.review import ReviewRunRequest
from app.tasks.review_runner import run_review_job
from app.tasks.watchdog import reap_zombie_reviews
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import health_check_key_suffix
from arq.cron import cron
from arq.jobs import Job, JobStatus

logger = get_logger(__name__)

#: ARQ 任务名（与 `run_review_job` 函数名一致，改函数名要同步改这里）
REVIEW_JOB_NAME = "run_review_job"

#: 入队结果，接口层据此决定响应文案与日志级别
QUEUED = "queued"
IN_PROCESS = "in_process"
REJECTED = "rejected"

#: 任务在队列里最多等多久：超过就丢弃，避免用户一小时前点的"重新审查"
#: 在系统空闲时突然跑起来，产出他自己都不再关心的报告
_QUEUE_TTL_SECONDS = 3_600

#: 进程内回退任务：不持有引用的 asyncio 任务可能被垃圾回收后静默消失
_spawned: set[asyncio.Task] = set()

_pool: ArqRedis | None = None
_pool_key: str = ""


async def shared_queue_pool(settings: Settings) -> ArqRedis | None:
    """取共享 ARQ 连接池；Redis 不可达时返回 None（调用方决定是否降级）。

    连接池按进程复用：每次入队新建连接，等于把队列自己变成新的泄露源。
    """
    global _pool, _pool_key

    if not settings.queue_enabled:
        return None

    key = f"{settings.redis_url}::{settings.queue_name}"
    if _pool is not None and _pool_key == key:
        return _pool

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    try:
        # 连不上就立刻返回 None 交给降级路径，不在请求线程里等重连
        _pool = await create_pool(redis_settings, default_queue_name=settings.queue_name)
    except Exception as exc:
        logger.warning("Redis 队列不可达（%s），本次退回进程内执行：%s", settings.redis_url, exc)
        _pool = None
        _pool_key = ""
        return None

    _pool_key = key
    logger.info("任务队列已连接：%s（队列 %s）", settings.redis_url, settings.queue_name)
    return _pool


async def enqueue_review(
    review_id: uuid.UUID,
    request: ReviewRunRequest,
    app_settings: Settings | None = None,
) -> str:
    """把一次审查投进队列。

    `_job_id` 用 review_id：同一份审查被重复点击时 ARQ 返回既有任务而不是排两次，
    避免两次并发写同一条 `ContractReview`。
    """
    settings = app_settings or get_settings()
    pool = await shared_queue_pool(settings)
    if pool is None:
        if not settings.queue_fallback_in_process:
            logger.error("队列不可用且已关闭进程内回退，审查 %s 未被受理", review_id)
            return REJECTED
        return spawn_in_process(review_id, request)

    try:
        job = await pool.enqueue_job(
            REVIEW_JOB_NAME,
            str(review_id),
            request.model_dump(mode="json", by_alias=True),
            _job_id=f"review:{review_id}",
            _queue_name=settings.queue_name,
            _expires=_QUEUE_TTL_SECONDS,
        )
    except Exception as exc:
        if not settings.queue_fallback_in_process:
            logger.error("入队失败且已关闭回退，审查 %s 未被受理：%s", review_id, exc)
            return REJECTED
        logger.warning("入队失败，退回进程内执行 review=%s：%s", review_id, exc)
        return spawn_in_process(review_id, request)

    if job is None:
        # 任务已在队列中（重复点击）：不算失败，也不重复执行
        logger.info("审查 %s 已在队列中，忽略重复提交", review_id)
        return QUEUED

    logger.info("审查任务已入队 review=%s queue=%s", review_id, settings.queue_name)
    return QUEUED


def spawn_in_process(review_id: uuid.UUID, request: ReviewRunRequest) -> str:
    """进程内执行（降级路径）。

    与 BackgroundTasks 的区别只在于这里明确记录了降级并保留了任务引用；
    它解决不了"进程重启丢任务"，所以只用于本地开发或 Redis 临时不可用。
    """
    logger.warning(
        "审查 %s 退回进程内执行：API 进程重启会丢失该任务（QUEUE_ENABLED 或 Redis 需排查）",
        review_id,
    )
    task = asyncio.create_task(run_review_job(None, str(review_id), request.model_dump(mode="json")))
    _spawned.add(task)
    task.add_done_callback(_spawned.discard)
    return IN_PROCESS


async def drain_in_process_tasks(timeout_seconds: float = 30.0) -> None:
    """退出前给在跑的任务一个收尾窗口，超时则放弃（由巡检兜底置 failed）。"""
    if not _spawned:
        return
    logger.info("等待 %s 个进程内审查任务收尾（最多 %.0fs）", len(_spawned), timeout_seconds)
    done, pending = await asyncio.wait(set(_spawned), timeout=timeout_seconds)
    del done
    for task in pending:
        task.cancel()


async def queue_health(settings: Settings) -> str:
    """健康检查用的队列状态，只 ping 已建立的共享池，不新建连接。"""
    if not settings.queue_enabled:
        return "in_process"
    pool = _pool if _pool_key == f"{settings.redis_url}::{settings.queue_name}" else None
    if pool is None:
        return "unavailable"
    try:
        await pool.ping()
    except Exception as exc:  # pragma: no cover - 取决于 Redis 状态
        logger.warning("Redis ping 失败：%s", exc)
        return "unavailable"
    return "connected"


async def queue_depth(settings: Settings) -> int | None:
    """队列积压条数；读不到时返回 None（即“未知”，不能当 0）。

    健康检查里报积压比报“连上了”有用：连上但没人消费（worker 没起）从 ping 看不出来。
    """
    pool = _pool if _pool_key == f"{settings.redis_url}::{settings.queue_name}" else None
    if pool is None:
        return None
    try:
        # ARQ 的队列是有序集合（zadd/zcard），不是 list：用 llen 会报 WRONGTYPE
        return int(await pool.zcard(settings.queue_name))
    except Exception as exc:  # pragma: no cover - 取决于 Redis 版本与状态
        logger.debug("读取队列积压失败：%s", exc)
        return None


async def worker_alive(settings: Settings) -> bool | None:
    """是否有活着的 worker 在消费队列。

    依据是 ARQ worker 自己写的心跳键（键名 = 队列名 + 后缀，TTL 为心跳周期的 2 倍）：
    只 ping Redis 看不出“队列通但没人消费”，而那种情形下审查会安静地排到天荒地老。
    返回 None 表示无法确认（没建连接池）。
    """
    pool = _pool if _pool_key == f"{settings.redis_url}::{settings.queue_name}" else None
    if pool is None:
        return None
    try:
        return bool(await pool.exists(settings.queue_name + health_check_key_suffix))
    except Exception as exc:  # pragma: no cover - 取决于 Redis 状态
        logger.debug("读取 worker 心跳键失败：%s", exc)
        return None


async def job_is_alive(review_id: uuid.UUID, settings: Settings) -> bool | None:
    """该审查的 ARQ 任务是否仍在队列里或正在执行。

    巡检用来区分“worker 没起来，任务还在排队”与“worker 跑到一半死了”：
    前者不能置为失败（否则会把正常积压的单子砍掉），后者才需要收尾。
    返回 None 表示无法确认（队列未启用或 Redis 不可达）。
    """
    if not settings.queue_enabled:
        return None
    pool = await shared_queue_pool(settings)
    if pool is None:
        return None
    try:
        job = Job(f"review:{review_id}", pool, _queue_name=settings.queue_name)
        status = await job.status()
    except Exception as exc:  # pragma: no cover - 取决于 Redis 状态
        logger.warning("查询任务 %s 状态失败，按“无法确认”处理：%s", review_id, exc)
        return None
    return status in {JobStatus.QUEUED, JobStatus.IN_PROGRESS, JobStatus.DEFERRED}


async def close_queue_pool() -> None:
    """释放连接池（API 与 worker 退出时都要调）。"""
    global _pool, _pool_key
    if _pool is not None:
        await _pool.aclose()
    _pool = None
    _pool_key = ""


def _cron_minutes(interval_seconds: int) -> set[int]:
    """把巡检周期换算成 ARQ cron 能表达的"每 N 分钟"。

    arq cron 只到分钟粒度，写 30s 会被悄悄放大成 1min，因此这里明确取整：
    亚分钟级巡检需求应该用进程内循环，而不是 cron。
    """
    step = max(1, min(59, interval_seconds // 60))
    return set(range(0, 60, step))


def build_worker_settings(app_settings: Settings | None = None) -> type:
    """按配置生成 ARQ `WorkerSettings`。"""
    settings = app_settings or get_settings()

    async def _on_startup(ctx: dict) -> None:
        del ctx  # arq 传入的运行上下文，本钩子不需要
        from app.core.logging import setup_logging
        from app.db.session import init_models

        setup_logging()
        # worker 与 API 一样需要先保证表存在（无 Alembic 阶段的开发期约定）
        try:
            await init_models()
        except Exception as exc:
            logger.error("worker 初始化表结构失败，任务将在执行时报错：%s", exc)
        logger.info(
            "审查 worker 已启动：并发=%s 硬超时=%ss 巡检=%ss",
            settings.queue_max_jobs,
            settings.queue_job_timeout_seconds,
            settings.zombie_watchdog_interval_seconds,
        )

    async def _on_shutdown(ctx: dict) -> None:
        del ctx
        from app.db.session import dispose_engine
        from app.services.llm.factory import aclose_shared_providers
        from app.services.rag.embeddings import aclose_shared_embedder
        from app.services.rag.rerank import aclose_shared_reranker
        from app.services.rag.sparse import aclose_shared_sparse_index

        await aclose_shared_providers()
        await aclose_shared_embedder()
        await aclose_shared_sparse_index()
        await aclose_shared_reranker()
        await dispose_engine()
        logger.info("审查 worker 退出，外部连接已释放")

    interval_minutes = _cron_minutes(settings.zombie_watchdog_interval_seconds)

    class WorkerSettings:
        """ARQ worker 配置。字段名由 arq 约定，不做包装。"""

        redis_settings = RedisSettings.from_dsn(settings.redis_url)
        queue_name = settings.queue_name
        # 用 tuple 而不是 list：类属性可变更对象会被 ruff 判为隐式共享状态，
        # 而 arq 只要求 Sequence，元组同样可用
        functions = (run_review_job,)
        # 巡检与审查跑在同一个 worker 里：省一个服务，且它只做一条 UPDATE
        cron_jobs = (
            cron(
                reap_zombie_reviews,
                name="reap-zombie-reviews",
                minute=interval_minutes,
                second={0},
                run_at_startup=True,
                timeout=120,
            ),
        )
        # 钩子名带下划线前缀定义：类体里直接写 `on_startup = staticmethod(on_startup)`
        # 会被当成先赋值后读取，外层函数变量根本读不到（NameError）
        on_startup = staticmethod(_on_startup)
        on_shutdown = staticmethod(_on_shutdown)
        job_timeout = settings.queue_job_timeout_seconds
        max_jobs = settings.queue_max_jobs
        # 心跳键 TTL = 本值 x 2：调短一点，“队列通但 worker 挂了”能更快被发现
        health_check_interval = 60
        # 只重试"执行异常"（进程级意外），业务失败由 `run_review_job` 自己消化
        max_tries = settings.queue_retry_attempts + 1
        keep_result = 3_600
        poll_delay = 1

    return WorkerSettings


#: 模块级入口，供 `arq app.tasks.queue.WorkerSettings` 与 `python -m app.tasks.worker` 发现；
#: 配置来自进程环境，因而 worker 容器与 API 容器读同一份 .env
WorkerSettings = build_worker_settings(get_settings())


__all__ = [
    "IN_PROCESS",
    "QUEUED",
    "REJECTED",
    "REVIEW_JOB_NAME",
    "WorkerSettings",
    "build_worker_settings",
    "close_queue_pool",
    "drain_in_process_tasks",
    "enqueue_review",
    "job_is_alive",
    "queue_depth",
    "queue_health",
    "shared_queue_pool",
    "spawn_in_process",
    "worker_alive",
]
