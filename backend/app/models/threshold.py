"""可动态调整的监管阈值口径。

阈值写在 `RedLineRule.threshold_value` 里只能覆盖"静态阈值"这一类，而
"一年期 LPR""综合成本红线""置信度路由阈值"这些口径是跨规则共用的行情数：
法务要按月更新，不能改代码、不能发版、也不能改环境变量后重启每个进程。

因此这里只存"口径"（key + 数值 + 生效日 + 来源），判定逻辑仍在规则引擎里；
`is_builtin` 之类结构属性不在本表，避免把规则与口径混成一张表。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RegulatoryThreshold(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """一条监管阈值口径（键值 + 生效日）。"""

    __tablename__ = "regulatory_thresholds"

    #: 取值见 app.services.rules.thresholds.ALLOWED_KEYS，避免自由拼写产生孤儿键
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    #: ratio=小数口径（0.24 表示 24%），multiple=倍数，count=条数
    unit: Mapped[str] = mapped_column(String(16), default="ratio", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 生效日：同一份合同在 LPR 调整后结论会不同，必须记住"当时用的是哪一天的数"
    effective_on: Mapped[date | None] = mapped_column(Date)
    updated_by: Mapped[str] = mapped_column(String(64), default="system", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
