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
    #: 两个通道各自的思考深度（环节3/4 与环节7 都要求“开启”）：
    #: 思考型模型不支持关闭思考，`default` 即“不下发 reasoning_effort、沿用供应商最深档”
    llm_reasoning_effort: str = "default"
    embedding_provider: str
    #: mock 向量时检索结果不具语义相关性，必须在响应里显式暴露
    embedding_is_mock: bool
    #: 稠密向量后端（qdrant）；权威文本仍以 Postgres 为单一数据源
    dense_store: str = "qdrant"
    #: 稀疏/重排通道的配置口径；ES 与 TEI 是否真连通以审查记录的
    #: `retrievalChannels` 为准（那里是本次实际参与通道，不是配置值）
    sparse_enabled: bool = False
    rerank_enabled: bool = False
    #: 监管罚单类比通道是否开启（默认关：罚单表可能还没数据）
    cases_enabled: bool = False
    #: 空串表示本次未配置独立复核通道；`rule-check-v1` 表示走的是确定性规则复核
    judge_model: str = ""
    #: llm | rule_check | disabled：复核到底以什么形态执行
    judge_mode: str = "disabled"
    #: False = 复核与主审同源，它的裁定不构成独立信号（回音壁）
    judge_independent: bool = False
    judge_reasoning_effort: str = ""
    #: 是否启用独立任务队列（false 表示审查在 API 进程内跑，重启会丢任务）
    queue_enabled: bool = True
    #: connected | unavailable | in_process
    queue_state: str = "unavailable"
    #: 队列积压条数；null = 无法确认（没建连接池）。连上但没人消费只会让积压长
    queue_depth: int | None = None
    #: 是否有活着的 worker 在消费（ARQ worker 心跳键）；null = 无法确认
    queue_worker_alive: bool | None = None
    #: 巡检判定僵尸的静默时长，运维可据此核对“队列硬超时 + 心跳”口径
    zombie_timeout_seconds: int = 0
    knowledge_provision_count: int = 0
    red_line_rule_count: int = 0
    #: “能跑但不该上线”的配置口径清单（mock 向量、同来路复核、队列降级等）
    config_warnings: list[str] = []


class PageMeta(CamelModel):
    total: int
    page: int
    page_size: int
