"""数据库连接与会话管理。"""

from __future__ import annotations

import re
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

#: 开发期新增列的幂等补丁：`create_all` 不会给已存在的表加列，
#: 而本项目还没接 Alembic，缺列会让写入在第一次审查时才报错。
#: 类型跟 ORM 一致（Postgres 上 JSONType 实际是 jsonb）
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "contract_reviews": {
        "judge_model": "VARCHAR(128) NOT NULL DEFAULT ''",
        "judge_call_count": "INTEGER NOT NULL DEFAULT 0",
        "judge_failed_count": "INTEGER NOT NULL DEFAULT 0",
        "retrieval_channels": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    },
    "review_findings": {
        "judge_verdict": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    },
}


async def ensure_added_columns() -> None:
    """补齐已存在表的新列；列已存在时 `ADD COLUMN IF NOT EXISTS` 是空操作。"""
    for table_name, columns in _ADDED_COLUMNS.items():
        for column_name, column_ddl in columns.items():
            sql = f'ALTER TABLE "{table_name}" ADD COLUMN IF NOT EXISTS "{column_name}" {column_ddl}'
            async with engine.begin() as conn:
                await conn.execute(text(sql))


#: 读取向量列的实际类型，format_type 会返回形如 vector(1024)
_VECTOR_COLUMN_TYPE_SQL = """
SELECT format_type(a.atttypid, a.atttypmod) AS column_type
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = :table_name AND n.nspname = 'public' AND a.attname = 'embedding' AND a.attnum > 0
"""


async def ensure_vector_extension() -> None:
    """确保 vector 扩展存在，否则 jsonb 之外的向量列无法创建。"""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


_VECTOR_DIMENSION_SQLS = (
    'DROP INDEX IF EXISTS "{index}"',
    'UPDATE "{table}" SET embedding = NULL WHERE embedding IS NOT NULL',
    'ALTER TABLE "{table}" ALTER COLUMN embedding TYPE vector({dimensions})',
)


async def align_vector_dimensions() -> int:
    """把向量列维度对齐到 `EMBEDDING_DIMENSIONS`，返回被改动的表数。

    pgvector 的维度写在列定义里，换嵌入模型（1536 → 1024）不会被 `create_all`
    自动修正，旧向量也不能直接转型。这里先清空向量（紧接着由启动时的
    `rebuild_missing_embeddings` 重算），再删索引、改列类型。
    """
    expected = settings.embedding_dimensions
    altered = 0

    for table_name in _VECTOR_TABLES:
        async with engine.connect() as conn:
            current = (
                await conn.execute(text(_VECTOR_COLUMN_TYPE_SQL), {"table_name": table_name})
            ).scalar()
        if current is None:
            continue

        match = re.search(r"vector\((\d+)\)", str(current))
        if match and int(match.group(1)) == expected:
            continue

        logger.warning(
            "向量列 %s.embedding 当前为 %s，重建为 vector(%s)；旧向量已清空，等待重算",
            table_name,
            current,
            expected,
        )
        statements = (
            sql.format(index=f"ix_{table_name}_embedding", table=table_name, dimensions=expected)
            for sql in _VECTOR_DIMENSION_SQLS
        )
        async with engine.begin() as conn:
            for sql in statements:
                await conn.execute(text(sql))
        altered += 1

    return altered


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
    await ensure_added_columns()
    await align_vector_dimensions()
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
