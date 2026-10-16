"""应用入口。

启动顺序有讲究：必须先建表并让 Qdrant 集合就绪，再写入内置红线与法条登记，
否则规则库为空的服务会把"没有红线"误呈现为"合同全部合规"。

API 进程只负责接单与入队；分钟级审查交给 worker 进程（`python -m app.tasks.worker`）。
只有当队列不可用而退回进程内执行时，本进程才自己跑巡检循环。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import close_app_clients
from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.db.session import dispose_engine, init_models, session_scope
from app.services.llm.factory import validate_model_slots
from app.services.rag.embeddings import (
    EmbeddingNotConfigured,
    shared_embedder,
)
from app.services.rag.ingest import ensure_registry, sync_sparse_index, sync_vector_store
from app.services.rag.sparse import shared_sparse_index
from app.services.rag.vectorstore import shared_vectorstore
from app.services.rules.seed import ensure_builtin_rules
from app.services.rules.thresholds import ensure_default_thresholds
from app.tasks.queue import (
    close_queue_pool,
    drain_in_process_tasks,
    shared_queue_pool,
)
from app.tasks.watchdog import watch_zombie_reviews_loop

settings = get_settings()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger.info("启动 %s（environment=%s）", settings.app_name, settings.environment)

    # 模型槽位先检查：`JUDGE_SAME_MODEL_POLICY=fail_fast` 时同来路配置直接拒绝启动，
    # 而不是运行中悄悄自我附和
    validate_model_slots(settings)

    # 队列先连上：健康检查只 ping 已有连接池，不为此新建连接
    pool = await shared_queue_pool(settings)
    if settings.queue_enabled and pool is None:
        logger.warning(
            "QUEUE_ENABLED=true 但 Redis（%s）不可达；审查将退回进程内执行，"
            "本进程重启会丢失在跑的任务",
            settings.redis_url,
        )

    watchdog_task: asyncio.Task | None = None
    if settings.zombie_watchdog_enabled and (not settings.queue_enabled or pool is None):
        # 队列跑不起来时没有 worker 执行 cron 巡检，必须由本进程兜住僵尸任务
        watchdog_task = asyncio.create_task(watch_zombie_reviews_loop())

    try:
        await init_models()

        embedder = None
        try:
            embedder = shared_embedder(settings)
        except EmbeddingNotConfigured as exc:
            logger.error("向量客户端无法初始化，检索将只走关键词通道：%s", exc)

        async with session_scope() as session:
            created_rules = await ensure_builtin_rules(session)
            created_thresholds = await ensure_default_thresholds(session, settings)
            created_provisions = await ensure_registry(session)

        # 稠密向量已迁 Qdrant：先确保集合按当前维度就绪（不符会重建清空），再全量重算。
        # mock 向量下不 populate（旧行为一致，且哈希向量无语义）；Qdrant 不可达只降级不阻塞启动
        synced_vectors = 0
        store = shared_vectorstore(settings)
        if embedder is not None and not settings.rag_use_mock_embedding:
            try:
                await store.ensure_collection(store.provisions_collection)
                if settings.rag_use_cases:
                    await store.ensure_collection(store.cases_collection)
                async with session_scope() as session:
                    synced_vectors = await sync_vector_store(session, store, embedder)
            except Exception as exc:
                logger.warning("Qdrant 向量同步未完成，稠密检索暂降级：%s", exc)

        logger.info(
            "规则与知识库初始化完成：新增红线 %s 条，新增阈值口径 %s 项，新增法条登记 %s 条，同步向量 %s 条",
            created_rules,
            created_thresholds,
            created_provisions,
            synced_vectors,
        )
        if settings.rag_use_mock_embedding:
            logger.warning(
                "当前为哈希 mock 向量，语义检索不可用；"
                "真实向量请设 RAG_USE_MOCK_EMBEDDING=false 并把 EMBEDDING_PROVIDER 指向 ollama/openai_compatible"
            )

        # ES 不可达只关掉一路召回，不能拖到规则库初始化一起失败
        sparse = shared_sparse_index(settings)
        if sparse is not None:
            try:
                async with session_scope() as session:
                    indexed = await sync_sparse_index(session, sparse)
                logger.info("BM25 索引已同步：%s 条", indexed)
            except Exception as exc:
                logger.warning("BM25 索引不可用，检索降级为直连+关键词+向量：%s", exc)
    except Exception as exc:
        # 数据库不可用时仍然把服务起起来，健康检查与接口会明确报告 degraded，
        # 便于先排查依赖而不是让容器反复崩溃重启
        logger.error("初始化失败，服务将以降级状态启动：%s", exc)

    for warning in settings.audit_warnings():
        logger.warning("配置口径：%s", warning)

    yield

    if watchdog_task is not None:
        watchdog_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watchdog_task
    # 进程内回退的任务给一个收尾窗口；超过就放弃，由巡检置 failed
    await drain_in_process_tasks()
    await close_queue_pool()
    # 模型、向量、BM25、重排四类外部连接统一在这里释放
    await close_app_clients()
    # 最后归还数据库连接：模块级引擎不随事件循环重启，留着连接只会得到
    # “Event loop is closed” 与下一轮 lifespan 的隐性失败
    await dispose_engine()
    logger.info("服务退出")


app = FastAPI(
    title=settings.app_name,
    description=(
        "信贷合同合规审查 Agent：规则引擎 + LLM/RAG 双通道校验、"
        "溯源引用核验与置信度路由人工复核。"
    ),
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/", tags=["meta"])
async def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "api": settings.api_v1_prefix,
        "docs": "/docs",
        "health": f"{settings.api_v1_prefix}/health",
    }
