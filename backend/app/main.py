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
from app.services.rag.embeddings import EmbeddingNotConfigured, build_embedding_client
from app.services.rag.ingest import ensure_registry, rebuild_missing_embeddings, sync_sparse_index
from app.services.rag.rerank import aclose_shared_reranker
from app.services.rag.sparse import aclose_shared_sparse_index, shared_sparse_index
from app.services.rules.seed import ensure_builtin_rules

settings = get_settings()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    logger.info("启动 %s（environment=%s）", settings.app_name, settings.environment)

    try:
        await init_models()

        embedder = None
        try:
            embedder = build_embedding_client(settings)
        except EmbeddingNotConfigured as exc:
            logger.error("向量客户端无法初始化，检索将只走关键词通道：%s", exc)

        async with session_scope() as session:
            created_rules = await ensure_builtin_rules(session)
            created_provisions = await ensure_registry(session, embedder=embedder)
            rebuilt = 0
            if embedder is not None and not settings.rag_use_mock_embedding:
                # 向量列对齐或新导入原文都可能导致空洞，启动时补一轮
                rebuilt = await rebuild_missing_embeddings(session, embedder)

        logger.info(
            "规则与知识库初始化完成：新增红线 %s 条，新增法条登记 %s 条，补算向量 %s 条",
            created_rules,
            created_provisions,
            rebuilt,
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

    yield
    await aclose_shared_sparse_index()
    await aclose_shared_reranker()
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
