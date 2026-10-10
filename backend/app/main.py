"""应用入口。

启动顺序有讲究：必须先建 pgvector 扩展与表，再写入内置红线与法条登记，
否则规则库为空的服务会把"没有红线"误呈现为"合同全部合规"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.db.session import init_models, session_scope
from app.services.rag.ingest import ensure_registry
from app.services.rules.seed import ensure_builtin_rules

settings = get_settings()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger.info("启动 %s（environment=%s）", settings.app_name, settings.environment)

    try:
        await init_models()
        async with session_scope() as session:
            created_rules = await ensure_builtin_rules(session)
            created_provisions = await ensure_registry(session)
        logger.info(
            "规则与知识库初始化完成：新增红线 %s 条，新增法条登记 %s 条",
            created_rules,
            created_provisions,
        )
        if settings.rag_use_mock_embedding:
            logger.warning(
                "当前为哈希 mock 向量，语义检索不可用；"
                "生产请配置 EMBEDDING_API_KEY 并将 RAG_USE_MOCK_EMBEDDING 设为 false"
            )
    except Exception as exc:
        # 数据库不可用时仍然把服务起起来，健康检查与接口会明确报告 degraded，
        # 便于先排查依赖而不是让容器反复崩溃重启
        logger.error("初始化失败，服务将以降级状态启动：%s", exc)

    yield
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
