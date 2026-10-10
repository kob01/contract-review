"""人工法务复核任务（Human-in-the-loop）。

复核结论是数据飞轮的输入：`expert_verdict` 与 `expert_risk_level`
记录了专家对模型判断的修正，可导出为后续 SFT/DPO 的标注样本。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import HumanReviewReason, HumanReviewStatus


class HumanReviewTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "human_review_tasks"
    __table_args__ = (Index("ix_human_review_tasks_status_created", "status", "created_at"),)

    finding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_findings.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contract_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True
    )

    reason: Mapped[str] = mapped_column(String(32), default=HumanReviewReason.MANUAL.value)
    status: Mapped[str] = mapped_column(
        String(24), default=HumanReviewStatus.PENDING.value, nullable=False
    )

    assignee: Mapped[str | None] = mapped_column(String(64))
    comment: Mapped[str | None] = mapped_column(Text)
    #: 专家结论：confirmed / false_positive / need_adjustment
    expert_verdict: Mapped[str | None] = mapped_column(String(32))
    expert_risk_level: Mapped[str | None] = mapped_column(String(16))
    #: 模型原始结论快照，导出训练样本时用于构造 prompt/label 对
    model_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exported_for_sft: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
