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

#: 历史上这两张表带 pgvector 的 embedding 列，迁到 Qdrant 后需清理遗留列
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
        # 僵尸巡检的判定依据：没有它就只能拿 updated_at 猜任务是否还在跑
        "heartbeat_at": "TIMESTAMPTZ",
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


async def drop_legacy_embedding_columns() -> None:
    """清理迁移前遗留的 pgvector `embedding` 列（幂等）。

    稠密向量已迁到 Qdrant，Postgres 不再存向量列；旧开发库里可能仍带着
    `embedding vector(n)` 列，`create_all` 不会删列，这里显式 DROP IF EXISTS。
    列不存在时是空操作。
    """
    for table_name in _VECTOR_TABLES:
        async with engine.begin() as conn:
            await conn.execute(
                text(f'ALTER TABLE "{table_name}" DROP COLUMN IF EXISTS "embedding"')
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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_added_columns()
    await drop_legacy_embedding_columns()


async def dispose_engine() -> None:
    """进程退出时释放连接池里的所有连接。

    不关池会留下两种隐形代价：一是已关闭事件循环里挂着可用连接（重启
    lifespan、跑集成测试、`--reload` 换 loop 时都会碰上 “Event loop is closed”）；
    二是退出阶段无法将连接干净归还给 Postgres。引擎是模块级单例，所以也只能在这里统一关。
    """
    await engine.dispose()


async def probe_database() -> str:
    """健康检查用的连通性探测。"""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "connected"
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        logger.warning("数据库连通性检查失败：%s", exc)
        return "unavailable"
