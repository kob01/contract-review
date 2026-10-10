"""数据库连接与会话管理。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import Base

logger = get_logger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

#: pgvector 扩展与向量索引必须在建表前后分别执行
_HNSW_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS {index_name}
ON {table_name} USING hnsw (embedding vector_cosine_ops)
"""

_IVFFLAT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS {index_name}
ON {table_name} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
"""

_VECTOR_TABLES = ("legal_provisions", "enforcement_cases")


async def ensure_vector_extension() -> None:
    """确保 vector 扩展存在，否则 jsonb 之外的向量列无法创建。"""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


async def ensure_vector_indexes() -> None:
    """按配置创建向量索引；索引类型写错时降级为无索引而不是启动失败。"""
    if settings.vector_index_type == "none":
        return

    template = (
        _HNSW_INDEX_SQL
        if settings.vector_index_type == "hnsw"
        else _IVFFLAT_INDEX_SQL
    )

    for table_name in _VECTOR_TABLES:
        index_name = f"ix_{table_name}_embedding"
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(template.format(index_name=index_name, table_name=table_name))
                )
        except Exception as exc:  # pragma: no cover - 取决于 pgvector 版本
            logger.warning(
                "创建向量索引 %s 失败（数据量小时全表扫描仍可用）：%s", index_name, exc
            )


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求级会话，异常自动回滚。"""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """非请求场景（后台任务、脚本）使用的会话边界。"""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """开发环境建表。生产环境请改用 Alembic 迁移。"""
    import app.models  # noqa: F401  # 触发模型注册

    await ensure_vector_extension()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_vector_indexes()


async def probe_database() -> str:
    """健康检查用的连通性探测。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "connected"
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        logger.warning("数据库连通性检查失败：%s", exc)
        return "unavailable"
