"""应用配置。

所有配置项均可通过环境变量覆盖，环境变量优先级高于 `.env` 文件。
`.env` 查找顺序：`backend/.env` → 仓库根 `.env`。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR / ".env", REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_name: str = "信贷合同合规审查系统"
    environment: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # ---------- 数据库 ----------
    database_url: str = (
        "postgresql+asyncpg://contract_review:contract_review@localhost:5432/contract_review"
    )
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---------- LLM（OpenAI 兼容接口）----------
    #: auto = 有密钥走真实模型，无密钥退回 Mock
    llm_provider: Literal["auto", "openai_compatible", "mock"] = "auto"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 4096
    llm_timeout_seconds: int = 120
    # 单次审查送入模型的最大字符数，超出按条款分批
    llm_max_chars_per_call: int = 12_000
    # 并发调用模型的上限，避免打爆供应商限速
    llm_max_concurrency: int = 3

    # ---------- 向量化与检索（RAG）----------
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    #: 无密钥时使用确定性哈希向量，保证离线可跑通链路（仅用于开发/测试）
    rag_use_mock_embedding: bool = True
    rag_top_k: int = 6
    #: 相似度下限，低于此分数不进提示词，避免拿不相关法条让模型“硬套”
    rag_min_similarity: float = 0.30
    #: pgvector 索引类型：none | ivfflat | hnsw
    vector_index_type: str = "hnsw"

    # ---------- 监管阈值（可配置，不写死在代码里）----------
    #: 一年期 LPR（小数，0.031 表示 3.1%），民间借贷司法保护上限 = 4 × 此值
    lpr_one_year_rate: float = 0.031
    #: 司法保护上限倍数
    lpr_multiple: float = 4.0
    #: 消费金融/小贷监管窗口指导综合成本红线
    regulatory_cost_ceiling: float = 0.24
    #: 历史高利贷参考线（36%），用于分级提示
    historical_hard_ceiling: float = 0.36
    #: 置信度路由阈值：低于此值的模型结论转人工复核
    confidence_routing_threshold: float = 0.80
    #: 高风险结论是否强制人工确认
    mandatory_human_review_for_high_risk: bool = True

    # ---------- 文件上传 ----------
    upload_dir: Path = BACKEND_DIR / "storage" / "contracts"
    max_upload_mb: int = 20
    allowed_upload_suffixes: list[str] = [".pdf", ".docx", ".txt", ".md"]

    # ---------- 监管公开信息采集 ----------
    #: 采集开关，默认关闭：爬取必须先确认目标站 robots 与使用条款
    ingestion_enabled: bool = False
    ingestion_user_agent: str = "contract-review-compliance-bot/0.1"
    ingestion_min_interval_seconds: float = 3.0
    ingestion_max_items_per_run: int = 50

    # ---------- 安全（占位，后续接入鉴权时使用）----------
    secret_key: str = "change-me-in-production"

    @field_validator("cors_origins", "allowed_upload_suffixes", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """允许在 `.env` 中用逗号分隔书写列表。"""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """带缓存的配置单例。"""
    settings = Settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
