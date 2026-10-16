"""健康检查。

不只回答"进程活着"，还要回答"这套审查链路当前是否真的可信"：
数据库是否连通、用的是真实模型还是 Mock、向量是不是哈希占位、
队列是否真的在收任务、复核模型是不是独立信号、红线库与法条库是否为空。
运维看一条响应即可判断降级程度。

这里不建任何外部客户端：模型/向量侧一律走 `describe_*` 纯函数读配置。
以前每次探测都新建一个 AsyncOpenAI 且不关闭，高并发下先耗尽的是文件描述符。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Settings, get_db, get_settings
from app.db.session import probe_database
from app.models.knowledge import LegalProvision
from app.models.redline import RedLineRule
from app.schemas.common import HealthResponse
from app.services.llm.factory import (
    JUDGE_MODE_LLM,
    describe_judge_backend,
    describe_llm_backend,
)
from app.services.rag.embeddings import describe_embedding_backend
from app.services.rag.vectorstore import describe_vector_store
from app.tasks.queue import queue_depth, queue_health, worker_alive

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> HealthResponse:
    database = await probe_database()

    provision_count = await session.scalar(
        select(func.count()).select_from(LegalProvision).where(LegalProvision.is_active)
    )
    rule_count = await session.scalar(
        select(func.count()).select_from(RedLineRule).where(RedLineRule.is_active)
    )

    embedder_name = describe_embedding_backend(app_settings)
    provider_name, provider_model, provider_effort = describe_llm_backend(app_settings)
    judge_slot = describe_judge_backend(app_settings)
    queue_state = await queue_health(app_settings)
    backlog = await queue_depth(app_settings)
    has_worker = await worker_alive(app_settings)

    warnings = app_settings.audit_warnings()
    if has_worker is False and queue_state == "connected":
        # “队列连通但没人消费”不报错，只会让审查永远排队，必须单独报出来
        warnings.append("任务队列已连通但没有 worker 心跳，审查将始终排队（请启动 worker 进程）")

    degraded = (
        database != "connected"
        or not rule_count
        or not provision_count
        or provider_name == "mock"
        # 哈希占位向量不承载语义：用它得出的引用相似度不可信，不能报 ok
        or app_settings.rag_use_mock_embedding
        # 队列声明启用却连不上：审查会退回进程内执行，发版即丢任务
        or (app_settings.queue_enabled and queue_state == "unavailable")
        # 生产环境里“没 worker 在消费”等于审查不可用，不能报 ok
        or (
            app_settings.is_prod
            and app_settings.queue_enabled
            and queue_state == "connected"
            and has_worker is False
        )
        # 生产环境里"同模型复核"属于治理缺陷，不能报 ok
        or (
            app_settings.is_prod
            and judge_slot.mode == JUDGE_MODE_LLM
            and not judge_slot.independent
        )
    )

    return HealthResponse(
        status="degraded" if degraded else "ok",
        app=app_settings.app_name,
        environment=app_settings.environment,
        version="0.1.0",
        database=database,
        llm_provider=provider_name,
        llm_model=provider_model,
        llm_reasoning_effort=provider_effort,
        embedding_provider=embedder_name,
        embedding_is_mock=embedder_name == "mock_hashing",
        dense_store=describe_vector_store(app_settings),
        sparse_enabled=app_settings.rag_sparse_enabled,
        rerank_enabled=app_settings.rag_rerank_enabled,
        cases_enabled=app_settings.rag_use_cases,
        judge_model=judge_slot.model,
        judge_mode=judge_slot.mode,
        judge_independent=judge_slot.independent,
        judge_reasoning_effort=(
            app_settings.judge_reasoning_effort if judge_slot.mode == JUDGE_MODE_LLM else ""
        ),
        queue_enabled=app_settings.queue_enabled,
        queue_state=queue_state,
        #: 积压条数：连上但没人消费（worker 未起）从 ping 看不出来，只能看积压
        queue_depth=backlog,
        #: 是否有活着的 worker 在消费（靠 ARQ worker 自己写的心跳键判）；null = 无法确认
        queue_worker_alive=has_worker,
        zombie_timeout_seconds=app_settings.effective_zombie_timeout_seconds,
        knowledge_provision_count=int(provision_count or 0),
        red_line_rule_count=int(rule_count or 0),
        config_warnings=warnings,
    )
