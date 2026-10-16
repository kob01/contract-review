"""法规与监管知识库。

两类数据：
- `LegalProvision`：法条级切片（一条一文），是"溯源引用"的唯一可信来源；
- `EnforcementCase`：监管处罚案例（罚单），用于"同类违规已被处罚"的类比论证。

二者都支持语义检索（稠密向量存于 Qdrant 加速层，向量列不在本表）；同时保留 law_name/article_no 的
精确匹配能力，因为"第几条"这类判断靠向量相似度并不可靠。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import KnowledgeDocType


class LegalProvision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条法律条文。"""

    __tablename__ = "legal_provisions"
    __table_args__ = (
        Index("ix_legal_provisions_law_article", "law_name", "article_no"),
        Index("ix_legal_provisions_doc_type_active", "doc_type", "is_active"),
    )

    doc_type: Mapped[str] = mapped_column(
        String(32), default=KnowledgeDocType.DEPARTMENT_RULE.value, nullable=False
    )
    law_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="法规名称")
    article_no: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="条号，例如“第二十一条”"
    )
    chapter: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(255), comment="条文主旨")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="条文原文")

    issuing_body: Mapped[str | None] = mapped_column(String(255))
    effective_on: Mapped[date | None] = mapped_column(Date)
    repealed_on: Mapped[date | None] = mapped_column(Date, comment="废止日期，非空即失效")
    version_label: Mapped[str | None] = mapped_column(
        String(64), comment="版本标识，例如“2024年修订”"
    )
    source_url: Mapped[str | None] = mapped_column(String(1024))

    #: 未逐条核验的占位样本，UI 必须显示"待核验"标识
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 与红线规则的关联度分值（入库时的启发式打分，用于优先展示）
    relevance_hint: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    chunk_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    def is_repealed(self, on: date | None = None) -> bool:
        if self.repealed_on is None:
            return False
        return (on or date.today()) >= self.repealed_on


class EnforcementCase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """监管机构公开的行政处罚案例（罚单）。"""

    __tablename__ = "enforcement_cases"
    __table_args__ = (Index("ix_enforcement_cases_decided_on", "decided_on"),)

    case_no: Mapped[str | None] = mapped_column(String(128))
    org_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="被处罚机构")
    penalty_body: Mapped[str | None] = mapped_column(String(255), comment="处罚机关")
    decided_on: Mapped[date | None] = mapped_column(Date)
    published_on: Mapped[date | None] = mapped_column(Date)

    violation_types: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False, comment="违规事实")
    penalty_content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    penalty_amount: Mapped[float | None] = mapped_column(Float, comment="罚款金额（元）")
    basis_refs: Mapped[list[dict]] = mapped_column(
        JSONType, default=list, nullable=False, comment="所依据的法条引用"
    )
    source_url: Mapped[str | None] = mapped_column(String(1024))

    is_sample: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def display_label(self) -> str:
        stamp = self.decided_on.isoformat() if self.decided_on else "日期未知"
        return f"{self.org_name} · {stamp}" + (f" · {self.case_no}" if self.case_no else "")


class KnowledgeIngestRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一次知识库采集/重建记录的审计信息。"""

    __tablename__ = "knowledge_ingest_runs"

    source: Mapped[str] = mapped_column(String(128), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ok_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
