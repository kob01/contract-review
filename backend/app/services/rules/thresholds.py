"""监管阈值口径的加载、校验与落库。

为什么把这几个数从配置搬进数据库：`LPR_ONE_YEAR_RATE` 按月变动，
把它留在 `.env` 里意味着每次更新都要改文件、重启 API 与每一个 worker，
而且没人能回答"上周那份报告当时用的是哪个口径"。入库后：

- 法务通过接口自助改，不用发版；
- 每条口径带 `effective_on` 与 `updated_by`，结论可复盘；
- 表里没有该键时退回 `Settings` 默认值，离线开发与首次启动仍然可用。

判定逻辑仍在 `engine.RuleEngine` 里，本模块只管"口径从哪来、取值合不合法"。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.threshold import RegulatoryThreshold

logger = get_logger(__name__)

SOURCE_DB = "db"
SOURCE_SETTINGS = "settings"

#: 可调口径的唯一清单：键名、对应配置字段、展示名、单位与合法区间。
#: 不在清单里的键一律拒绝写入，避免自由拼写出一个永远不会被读到的孤儿键。
THRESHOLD_SPECS: dict[str, dict[str, object]] = {
    "lpr_one_year_rate": {
        "attr": "lpr_one_year_rate",
        "label": "一年期 LPR",
        "unit": "ratio",
        "min": 0.0,
        "max": 0.2,
        "description": "全国银行间同业拆借中心公布的一年期贷款市场报价利率（小数口径）",
    },
    "lpr_multiple": {
        "attr": "lpr_multiple",
        "label": "司法保护上限倍数",
        "unit": "multiple",
        "min": 1.0,
        "max": 10.0,
        "description": "民间借贷利率司法保护上限 = 一年期 LPR × 本倍数",
    },
    "regulatory_cost_ceiling": {
        "attr": "regulatory_cost_ceiling",
        "label": "综合融资成本红线",
        "unit": "ratio",
        "min": 0.0,
        "max": 1.0,
        "description": "消费金融/小贷监管窗口指导口径下的综合年化融资成本上限",
    },
    "historical_hard_ceiling": {
        "attr": "historical_hard_ceiling",
        "label": "历史高利贷参考线",
        "unit": "ratio",
        "min": 0.0,
        "max": 1.0,
        "description": "24%/36% 两档里的第二档，用于分级提示而非直接定性",
    },
    "confidence_routing_threshold": {
        "attr": "confidence_routing_threshold",
        "label": "置信度路由阈值",
        "unit": "ratio",
        "min": 0.0,
        "max": 1.0,
        "description": "纯模型结论的置信度低于该值时强制转人工法务复核",
    },
}

ALLOWED_KEYS = tuple(THRESHOLD_SPECS)


class ThresholdValueError(ValueError):
    """阈值取值超出合法区间。"""


def validate_threshold_value(key: str, value: float) -> None:
    """按清单校验取值范围，并守住"历史参考线必须高于监管红线"这类组合关系。"""
    spec = THRESHOLD_SPECS.get(key)
    if spec is None:
        raise ThresholdValueError(
            f"未知阈值键 {key!r}，可调整的为：{', '.join(ALLOWED_KEYS)}"
        )
    lower = float(spec["min"])
    upper = float(spec["max"])
    if not lower < value <= upper:
        raise ThresholdValueError(
            f"{spec['label']} = {value} 超出合法区间 ({lower}, {upper}]；"
            "ratio 口径请写成小数（0.24 表示 24%），不要写 24"
        )


def _validate_cross_fields(values: dict[str, float]) -> None:
    historical = values.get("historical_hard_ceiling")
    regulatory = values.get("regulatory_cost_ceiling")
    if historical is not None and regulatory is not None and historical <= regulatory:
        raise ThresholdValueError(
            "历史高利贷参考线必须高于综合融资成本红线，否则分级提示永远不成立"
        )


@dataclass(slots=True, frozen=True)
class Thresholds:
    """一轮判定实际使用的口径集合。

    `sources` 记下每个数从哪来（库或配置默认），报告页才能回答
    "这条结论是按哪一天的 LPR 判的"。
    """

    lpr_one_year_rate: float
    lpr_multiple: float
    regulatory_cost_ceiling: float
    historical_hard_ceiling: float
    confidence_routing_threshold: float
    sources: dict[str, str] = field(default_factory=dict)
    effective_on: dict[str, str] = field(default_factory=dict)
    updated_by: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Settings) -> Thresholds:
        return cls(
            lpr_one_year_rate=settings.lpr_one_year_rate,
            lpr_multiple=settings.lpr_multiple,
            regulatory_cost_ceiling=settings.regulatory_cost_ceiling,
            historical_hard_ceiling=settings.historical_hard_ceiling,
            confidence_routing_threshold=settings.confidence_routing_threshold,
            sources=dict.fromkeys(ALLOWED_KEYS, SOURCE_SETTINGS),
        )

    @property
    def judicial_protection_ceiling(self) -> float:
        return round(self.lpr_one_year_rate * self.lpr_multiple, 6)

    def value_of(self, key: str) -> float:
        return getattr(self, key)

    def rows(self) -> list[dict]:
        """给接口与报告页展示的逐条口径。"""
        rows: list[dict] = []
        for key, spec in THRESHOLD_SPECS.items():
            rows.append(
                {
                    "key": key,
                    "label": str(spec["label"]),
                    "unit": str(spec["unit"]),
                    "value": self.value_of(key),
                    "min": float(spec["min"]),
                    "max": float(spec["max"]),
                    "description": str(spec.get("description") or ""),
                    "source": self.sources.get(key, SOURCE_SETTINGS),
                    "effectiveOn": self.effective_on.get(key),
                    "updatedBy": self.updated_by.get(key),
                }
            )
        return rows


async def ensure_default_thresholds(session: AsyncSession, settings: Settings) -> int:
    """把配置默认值登记为库内口径（已存在的键不覆盖）。

    与内置红线同一套逻辑：库里的值是法务改过的，启动不能把它抹平。
    """
    existing = {
        key
        for (key,) in (await session.execute(select(RegulatoryThreshold.key))).all()
    }
    created = 0
    for key, spec in THRESHOLD_SPECS.items():
        if key in existing:
            continue
        session.add(
            RegulatoryThreshold(
                key=key,
                label=str(spec["label"]),
                value=float(getattr(settings, str(spec["attr"]))),
                unit=str(spec["unit"]),
                description=str(spec.get("description") or ""),
                updated_by="system",
                is_active=True,
            )
        )
        created += 1

    if created:
        await session.commit()
        logger.info("写入监管阈值口径 %s 条（取自配置默认值）", created)
    return created


async def load_thresholds(session: AsyncSession, settings: Settings) -> Thresholds:
    """加载生效口径：库内记录覆盖配置默认值。"""
    baseline = Thresholds.from_settings(settings)
    if not settings.thresholds_from_db:
        return baseline

    result = await session.execute(
        select(RegulatoryThreshold)
        .where(RegulatoryThreshold.is_active)
        .where(RegulatoryThreshold.key.in_(list(ALLOWED_KEYS)))
    )
    rows = list(result.scalars().all())
    if not rows:
        return baseline

    values: dict[str, float] = {key: baseline.value_of(key) for key in ALLOWED_KEYS}
    sources = dict(baseline.sources)
    effective_on: dict[str, str] = {}
    updated_by: dict[str, str] = {}
    for row in rows:
        if row.key not in values:
            continue
        values[row.key] = row.value
        sources[row.key] = SOURCE_DB
        effective_on[row.key] = row.effective_on.isoformat() if row.effective_on else ""
        updated_by[row.key] = row.updated_by

    _validate_cross_fields(values)
    return Thresholds(
        lpr_one_year_rate=values["lpr_one_year_rate"],
        lpr_multiple=values["lpr_multiple"],
        regulatory_cost_ceiling=values["regulatory_cost_ceiling"],
        historical_hard_ceiling=values["historical_hard_ceiling"],
        confidence_routing_threshold=values["confidence_routing_threshold"],
        sources=sources,
        effective_on=effective_on,
        updated_by=updated_by,
    )


async def upsert_thresholds(
    session: AsyncSession,
    updates: Iterable[tuple[str, float, date | None]],
    *,
    updated_by: str,
    settings: Settings,
) -> Thresholds:
    """写入若干条口径并返回最新生效集合。

    校验发生在写之前：一批里有一条越界就整批拒绝，避免出现
    "LPR 改了、倍数没改"这种比不改更危险的半截状态。
    """
    pending = list(updates)
    if not pending:
        return await load_thresholds(session, settings)

    current = await load_thresholds(session, settings)
    merged = {key: current.value_of(key) for key in ALLOWED_KEYS}
    for key, value, _ in pending:
        validate_threshold_value(key, value)
        merged[key] = value
    _validate_cross_fields(merged)

    existing = {
        row.key: row
        for row in (
            await session.execute(
                select(RegulatoryThreshold).where(
                    RegulatoryThreshold.key.in_([key for key, _, _ in pending])
                )
            )
        ).scalars().all()
    }

    for key, value, effective_on in pending:
        spec = THRESHOLD_SPECS[key]
        row = existing.get(key)
        if row is None:
            session.add(
                RegulatoryThreshold(
                    key=key,
                    label=str(spec["label"]),
                    value=value,
                    unit=str(spec["unit"]),
                    description=str(spec.get("description") or ""),
                    effective_on=effective_on,
                    updated_by=updated_by,
                    is_active=True,
                )
            )
            continue
        row.value = value
        row.effective_on = effective_on or date.today()
        row.updated_by = updated_by
        row.is_active = True

    await session.commit()
    logger.info(
        "监管阈值已更新：%s（操作人 %s）",
        ", ".join(f"{key}={value}" for key, value, _ in pending),
        updated_by,
    )
    return await load_thresholds(session, settings)
