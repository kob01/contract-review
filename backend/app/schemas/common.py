"""通用 Schema。

对外统一 camelCase（前端直接消费），入站同时接受 snake_case。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """字段名 snake_case、序列化名 camelCase 的模型基类。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class HealthResponse(CamelModel):
    status: str = Field(description="ok / degraded")
    app: str
    environment: str
    version: str
    database: str = Field(description="PostgreSQL 连通性探测结果")
    llm_provider: str = Field(description="openai_compatible / mock")
    llm_model: str
    embedding_provider: str
    #: mock 向量时检索结果不具语义相关性，必须在响应里显式暴露
    embedding_is_mock: bool
    #: 稀疏/重排通道的配置口径；ES 与 TEI 是否真连通以审查记录的
    #: `retrievalChannels` 为准（那里是本次实际参与通道，不是配置值）
    sparse_enabled: bool = False
    rerank_enabled: bool = False
    #: 空串表示本次未配置独立复核模型
    judge_model: str = ""
    knowledge_provision_count: int = 0
    red_line_rule_count: int = 0


class PageMeta(CamelModel):
    total: int
    page: int
    page_size: int
