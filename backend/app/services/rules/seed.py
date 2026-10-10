"""红线规则的加载与落库。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.redline import RedLineRule
from app.services.rules.builtin_redlines import as_rule_rows

logger = get_logger(__name__)


async def ensure_builtin_rules(session: AsyncSession) -> int:
    """补齐缺失的内置红线。

    已存在的编码不覆盖，避免抹掉法务在库内做的停用/阈值调整；
    内置规则的定义变更需要通过规则管理接口或迁移脚本显式同步。
    """
    existing = {
        code
        for (code,) in (await session.execute(select(RedLineRule.code))).all()
    }

    created = 0
    for row in as_rule_rows():
        if row["code"] in existing:
            continue
        session.add(RedLineRule(**row))
        created += 1

    if created:
        await session.commit()
        logger.info("写入内置合规红线 %s 条", created)
    return created


async def load_active_rules(session: AsyncSession) -> list[RedLineRule]:
    """读取启用中的红线规则，按优先级升序（数字越小越先判定）。"""
    result = await session.execute(
        select(RedLineRule).where(RedLineRule.is_active).order_by(RedLineRule.priority)
    )
    return list(result.scalars().all())


async def rules_for_prompt(session: AsyncSession, codes: list[str]) -> list[RedLineRule]:
    """按编码取规则，用于把"规则无法定量判定"的条目注入模型提示词。"""
    if not codes:
        return []
    result = await session.execute(select(RedLineRule).where(RedLineRule.code.in_(codes)))
    rows = list(result.scalars().all())
    order = {code: index for index, code in enumerate(codes)}
    return sorted(rows, key=lambda rule: order.get(rule.code, 999))
