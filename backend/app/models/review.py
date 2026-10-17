"""审查任务、风险发现与溯源引用。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    ClauseCategory,
    CrossCheckStatus,
    FindingSource,
    HumanReviewStatus,
    ReviewStatus,
    RiskLevel,
)

if TYPE_CHECKING:
    from app.models.contract import Contract


class ContractReview(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次审查运行。

    保存模型、提示词版本、检索快照与双通道计数，使任一结论可被复盘：
    "哪条规则命中 / 模型看了哪些法条 / 为什么这条转人工"。
    """

    __tablename__ = "contract_reviews"
    __table_args__ = (Index("ix_contract_reviews_contract_status", "contract_id", "status"),)

    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(
        String(32), default=ReviewStatus.PENDING.value, nullable=False
    )
    llm_model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    #: 结论复核（LLM judge）所用模型；空串表示本次未执行复核
    judge_model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    judge_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 复核失败的批次数量：不能把“没复核成”隐呈现为“复核通过”
    judge_failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 本次实际参与召回的通道（exact_ref/vector/bm25/keyword/rerank）。
    #: 降级必须可事后审计：不能把“ES 挂了只走了关键词”呈现成混合检索结果
    retrieval_channels: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    #: 审查立场与范围快照（JSON），便于复现同一次运行
    run_options: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    #: 本次实际进入提示词的法条快照，报告页用作"参考依据"并支持事后审计
    retrieved_provisions: Mapped[list[dict]] = mapped_column(JSONType, default=list, nullable=False)

    risk_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    high_risk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    medium_risk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_risk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 双通道统计，交叉校验的可观测指标
    rule_hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_finding_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retrieved_chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_call_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_batch_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: 置信度路由结果
    min_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    needs_human_review: Mapped[bool] = mapped_column(default=False, nullable=False)
    pending_human_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 存活心跳：由 worker 在任务执行期间周期性刷新。
    #: 巡检不能拿 updated_at 判僵尸：一轮审查可能十几分钟不写库，
    #: 没有独立心跳字段就只能“把慢任务当死任务杀掉”或者“让死任务假活几小时”
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---------- 评测埋点（只观测不参与业务判定）----------
    #: 各阶段墙钟耗时 {"rules":1.2,"retrieval":3.4,"llm":88.1,...} 与总量、已计账比例。
    #: 只有一个 `elapsed_seconds` 就无法定位瓶颈在检索还是模型，也就没有 PAR-14/RET-14/GEN-16
    stage_timings: Mapped[dict] = mapped_column(
        JSONType, default=dict, nullable=False, comment="阶段耗时与降级留痕（评测用）"
    )
    #: token 用量按槽位分账：主审与复核是两家供应商，混算得出不了"每份合同成本"
    token_usage: Mapped[dict] = mapped_column(
        JSONType, default=dict, nullable=False, comment="主审/复核分槽 token 与延时汇总"
    )
    #: 本次运行发生过的降级点清单（通道缺失、批次失败、定位回退…）
    degradations: Mapped[list[dict]] = mapped_column(
        JSONType, default=list, nullable=False, comment="降级留痕（ORCH-06）"
    )

    contract: Mapped[Contract] = relationship(back_populates="reviews")
    findings: Mapped[list[ReviewFinding]] = relationship(
        back_populates="review",
        cascade="all, delete-orphan",
        order_by="ReviewFinding.order_index",
    )


class ReviewFinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """单条合规结论。

    `citations` 保存溯源结构：
    `[{"provision_id","law_name","article_no","quote","doc_type","verified"}]`，
    `verified=False` 表示模型给出的依据在知识库中未找到对应条文，
    前端必须标红提示"引用未核验"，而不是直接展示为既定事实。
    """

    __tablename__ = "review_findings"
    __table_args__ = (
        Index("ix_review_findings_review_risk", "review_id", "risk_level"),
        Index("ix_review_findings_human_status", "human_review_status"),
    )

    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contract_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )

    risk_level: Mapped[str] = mapped_column(String(16), default=RiskLevel.LOW.value, nullable=False)
    clause_category: Mapped[str] = mapped_column(
        String(32), default=ClauseCategory.OTHER.value, nullable=False
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False, comment="合同原文引用")
    analysis: Mapped[str] = mapped_column(Text, default="", nullable=False, comment="合规分析")
    suggestion: Mapped[str] = mapped_column(Text, default="", nullable=False, comment="修改建议")
    rewritten_text: Mapped[str | None] = mapped_column(Text, comment="建议改写后的条款表述")

    # ---------- 来源与交叉校验 ----------
    source: Mapped[str] = mapped_column(String(16), default=FindingSource.LLM.value, nullable=False)
    cross_check_status: Mapped[str] = mapped_column(
        String(24), default=CrossCheckStatus.LLM_ONLY.value, nullable=False
    )
    rule_code: Mapped[str | None] = mapped_column(String(64), comment="命中的红线规则编码")
    metric_snapshot: Mapped[dict] = mapped_column(
        JSONType, default=dict, nullable=False, comment="规则判定所用的指标值快照"
    )

    # ---------- 置信度路由 ----------
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    llm_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rule_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # ---------- 原文定位（PDF.js 高亮）----------
    segment_index: Mapped[int | None] = mapped_column(Integer)
    page_no: Mapped[int | None] = mapped_column(Integer)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)

    citations: Mapped[list[dict]] = mapped_column(JSONType, default=list, nullable=False)
    #: 同类违规的监管罚单 id（类比参考，仅在 RAG_USE_CASES 打开时非空）
    similar_case_ids: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    # ---------- 结论复核（LLM judge）----------
    #: 复核裁定快照：{judged, agree, citationOk, suggestedRiskLevel, confidence, reason, judgedBy}
    judge_verdict: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    # ---------- 人工复核 ----------
    human_review_status: Mapped[str] = mapped_column(
        String(24), default=HumanReviewStatus.NOT_REQUIRED.value, nullable=False
    )
    human_reviewer_id: Mapped[str | None] = mapped_column(String(64))
    human_comment: Mapped[str | None] = mapped_column(Text)
    adjusted_risk_level: Mapped[str | None] = mapped_column(String(16))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---------- 评测埋点 ----------
    #: 规则自带的阻断属性。以前只活在 `CandidateFinding` 上、没落库，
    #: 于是"硬阻断红线是否 100% 拦住"（RULE-03）只能回头 join 规则表去猜
    hard_block: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="所属红线是否硬阻断"
    )
    #: 结论级引用核验状态（verified/partial/placeholder/not_in_kb/none）。
    #: 核验事实原本只藏在 citations JSON 里，无法用 SQL 聚合算引用已核验率
    verification_status: Mapped[str] = mapped_column(
        String(24), default="none", nullable=False, comment="引用核验状态（GEN-06）"
    )
    #: 原文定位手段（exact/normalized/segment_fallback/evidence/failed）
    locate_method: Mapped[str] = mapped_column(
        String(24), default="failed", nullable=False, comment="回连原文的手段（GEN-12）"
    )

    order_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    review: Mapped[ContractReview] = relationship(back_populates="findings")

    @property
    def risk_enum(self) -> RiskLevel:
        return RiskLevel(self.risk_level)

    @property
    def has_unverified_citation(self) -> bool:
        return any(not citation.get("verified", False) for citation in self.citations or [])
