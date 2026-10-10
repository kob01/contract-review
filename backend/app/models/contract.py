"""合同主表。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.review import ContractReview

from app.db.base import Base, JSONType, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ContractKind, ContractStatus


class Contract(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一份被上传并解析的信贷类合同。

    - `content_text`：抽取出的全文，前端 PDF.js 定位高亮时按 `char_start/char_end` 反查；
    - `segments`：条款级切片 `[{"index","title","text","page_no","char_start","char_end"}]`；
    - `extracted_metrics`：规则引擎抽取的数值指标（年化利率、综合融资成本、罚息上浮比例等），
      与结论分开保存，便于复核"规则看到的是同一个数"。
    """

    __tablename__ = "contracts"
    __table_args__ = (Index("ix_contracts_status_created_at", "status", "created_at"),)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    contract_kind: Mapped[str] = mapped_column(
        String(32), default=ContractKind.UNKNOWN.value, nullable=False
    )
    lender_name: Mapped[str | None] = mapped_column(String(255), comment="贷款人/资金方")
    borrower_name: Mapped[str | None] = mapped_column(String(255), comment="借款人/消费者")
    guarantee_provider: Mapped[str | None] = mapped_column(String(255), comment="担保方")

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    file_suffix: Mapped[str] = mapped_column(String(16), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    content_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    segments: Mapped[list[dict]] = mapped_column(JSONType, default=list, nullable=False)
    extracted_metrics: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), default=ContractStatus.UPLOADED.value, nullable=False
    )
    parse_error: Mapped[str | None] = mapped_column(Text)
    #: 上传者标识。鉴权模块落地后可改为对 users 表的外键
    owner_id: Mapped[str | None] = mapped_column(String(64), index=True)

    reviews: Mapped[list[ContractReview]] = relationship(
        back_populates="contract",
        cascade="all, delete-orphan",
        order_by="ContractReview.created_at.desc()",
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<Contract {self.id} title={self.title!r} status={self.status}>"
