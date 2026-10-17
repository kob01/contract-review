"""评测埋点的持久层：模型调用与检索过程的逐次留痕。

为什么单开两张表而不是塞进 `ContractReview` 的 JSON 快照：
评测要按"批次 × 通道 × 条文"聚合（Recall@k、精排误杀率、token 成本分位），
JSON 整块读回内存再算，几百份审查就变成全表扫描；行级表可以直接用 SQL 分组。

这两张表只写不读业务逻辑：审查结论不依赖它们，删光也不影响出报告，
所以任何写入失败都绝不允许把一次审查带下水（见 `review_pipeline._persist_telemetry`）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, UUIDPrimaryKeyMixin
from app.models.enums import ModelSlot


class ReviewLlmCall(UUIDPrimaryKeyMixin, Base):
    """一次模型补全调用的观测记录（主审与复核都进来）。

    对应评测指标：GEN-01 输出契约合格率、GEN-02 空正文/截断率、GEN-16 成本与时延、
    ORCH-07 阶段时延。这些数据在 `LLMResult` 里本来就有，之前是被丢弃的。
    """

    __tablename__ = "review_llm_calls"
    __table_args__ = (
        Index("ix_review_llm_calls_review_slot", "review_id", "slot"),
    )

    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contract_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: review / judge：两个槽位的成本与失败率必须能分开算
    slot: Mapped[str] = mapped_column(
        String(16), default=ModelSlot.REVIEW.value, nullable=False
    )
    #: 第几批（从 1 开始）；与 `ReviewFinding.segment_index` 无关，用于位置衰减分析
    batch_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(String(16), default="default", nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)

    latency_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 思考型模型的关键观测项：思考 token 撞满 max_tokens 就是"200 但正文为空"的成因
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finish_reason: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    chars_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chars_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 本批实际进入提示词的条文 id：没有它就算不出"模型看了却没引用"（GEN-08 覆盖率分母）
    provisions_in_prompt: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    #: 原始响应正文：受 `EVAL_STORE_RAW_OUTPUT` 控制，用于重解析与幻觉复现
    raw_content: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ReviewRetrievalEvent(UUIDPrimaryKeyMixin, Base):
    """一批检索里"某条条文在某通道排第几、融合后第几、精排留没留"的留痕。

    对应评测指标：RET-01 Recall@k、RET-02 逐通道独占贡献、RET-05 精排误杀率、
    RET-06 精排有效性、RET-08 占位混入率、RET-11 长文位置衰减。
    """

    __tablename__ = "review_retrieval_events"
    __table_args__ = (
        Index("ix_review_retrieval_events_review_batch", "review_id", "batch_no"),
    )

    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contract_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_no: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 本批检索查询串（`_retrieval_query` 的产物）：不存就永远无法复盘"为什么没召回"
    query: Mapped[str] = mapped_column(Text, default="", nullable=False)

    provision_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    law_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    article_no: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    doc_type: Mapped[str] = mapped_column(String(48), default="", nullable=False)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: {"vector": 3, "bm25": 1, "keyword": 5, "exact_ref": 0}：融合前各通道名次
    rank_by_channel: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    #: 最终归属的召回方式列表（含 rerank 标记）
    match_kinds: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    fused_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: 精排分；None = 精排没给它打分（被候选上限截掉或被剔除）
    rerank_score: Mapped[float | None] = mapped_column(Float)

    in_prompt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dropped_by_rerank: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 与本批条款的词面重叠率：`_rank_provisions` 的实际排序键之一，存下来才能验证它有没有用
    lexical_overlap: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
