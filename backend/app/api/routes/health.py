"""健康检查。

不只回答"进程活着"，还要回答"这套审查链路当前是否真的可信"：
数据库是否连通、用的是真实模型还是 Mock、向量是不是哈希占位、
红线库与法条库是否为空。运维看一条响应即可判断降级程度。
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
from app.services.llm.factory import build_provider
from app.services.rag.embeddings import describe_embedding_backend

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
    provider = build_provider(app_settings)

    degraded = (
        database != "connected"
        or not rule_count
        or not provision_count
        or app_settings.llm_provider == "mock"
        or provider.name == "mock"
        # 哈希占位向量不承载语义：用它得出的引用相似度不可信，不能报 ok
        or app_settings.rag_use_mock_embedding
    )

    return HealthResponse(
        status="degraded" if degraded else "ok",
        app=app_settings.app_name,
        environment=app_settings.environment,
        version="0.1.0",
        database=database,
        llm_provider=provider.name,
        llm_model=provider.model,
        embedding_provider=embedder_name,
        embedding_is_mock=embedder_name == "mock_hashing",
        sparse_enabled=app_settings.rag_sparse_enabled,
        rerank_enabled=app_settings.rag_rerank_enabled,
        judge_model=app_settings.judge_model if app_settings.judge_enabled else "",
        knowledge_provision_count=int(provision_count or 0),
        red_line_rule_count=int(rule_count or 0),
    )
