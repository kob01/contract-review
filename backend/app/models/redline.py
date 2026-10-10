"""合规红线规则（可机器判定的硬指标）。

规则以数据形式保存，判定逻辑由 `app.services.rules.engine` 解释执行。
把"利率上限"这类硬指标写成数据而不是写成提示词，是整个架构的关键：
LLM 算错一个百分数没人发现，规则引擎算错则可以复现、可以回归测试。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ClauseCategory, MetricOperator, RiskLevel


class RedLineRule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "red_line_rules"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(
        String(32), default=ClauseCategory.OTHER.value, nullable=False
    )
    severity: Mapped[str] = mapped_column(String(16), default=RiskLevel.MEDIUM.value, nullable=False)

    #: 适用的合同种类（ContractKind），空列表表示全部
    applies_to: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    # ---------- 硬指标判定 ----------
    #: 指标键，取值见 app.services.rules.metrics 的指标与特征键
    metric_key: Mapped[str | None] = mapped_column(String(64))
    operator: Mapped[str | None] = mapped_column(String(12))
    #: 阈值来源：static 直接用 threshold_value；其余在判定时从配置解析，
    #: 使“4 倍 LPR”这类随行情变动的上限无需改规则数据
    threshold_source: Mapped[str] = mapped_column(
        String(32), default="static", nullable=False
    )
    threshold_value: Mapped[float | None] = mapped_column(Float)
    #: percent=阈值按百分数书写（24 表示 24%），ratio=按小数（0.24）
    unit: Mapped[str] = mapped_column(String(16), default="ratio", nullable=False)
    #: 容忍度，避免四舍五入误报（如 0.0005）
    tolerance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: 二次阈值，用于"历史红线"这类分级提示（如 36%）
    secondary_threshold: Mapped[float | None] = mapped_column(Float)

    # ---------- 文本特征判定（无需用数的霸王条款）----------
    #: 正则表达式列表，命中即产生弱信号
    keyword_patterns: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    # ---------- 通道编排 ----------
    #: True 表示硬拦截：直接定性违规，LLM 结论不得推翻它
    hard_block: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: True 表示规则无法定量判定时，把上下文交给 LLM + RAG 继续判断
    llm_fallback: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 命中时的置信度（规则引擎默认 1.0）
    hit_confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    # ---------- 溯源与提示词 ----------
    basis_refs: Mapped[list[dict]] = mapped_column(
        JSONType, default=list, nullable=False, comment="引用法条，入库前须人工核验"
    )
    guidance_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    suggestion_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def operator_enum(self) -> MetricOperator | None:
        return MetricOperator(self.operator) if self.operator else None

    @property
    def has_metric_condition(self) -> bool:
        return bool(self.metric_key and self.operator)
