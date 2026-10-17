"""配置漂移审计（RULE-06 / RULE-07 / RET-08 / DAT-05）。

为什么要有这个脚本：这个项目有**三个静默真理源**，代码改了而运行时行为不变是常态。

1. `red_line_rules` 表：`ensure_builtin_rules` 只补缺失编码、绝不覆盖已有行
   （为了保住法务在库内的手工调整）。于是 `builtin_redlines.py` 里改过的正则
   在线上根本不生效——本脚本 2026-10-17 首次实跑就抓到 BRCH-002 库里仍是
   没有否定词守卫的旧正则，评测因此报出一条真实误报，而单测一直是绿的。
2. `regulatory_thresholds` 表：`THRESHOLDS_FROM_DB=true` 时判定读库内生效值，
   `.env` 写了也不算。LPR 过期一个月，民间借贷 4×LPR 整片漏报。
3. `legal_provisions` 表：原文没导入时 `is_sample=True`，所有引用都判"未核验"。
   这不是故障（设计如此），但报告必须给出比例，否则会把"知识库欠账"当成"模型幻觉率"。

用法：

    cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=. uv run python ../scripts/audit_config_drift.py
    # 只看某类：--only rules|thresholds|knowledge
    # 确认要按代码库口径同步后才加 --fix（会写库，且只同步被判定为漂移的字段）

退出码 0 = 无漂移；1 = 有漂移；2 = 连不上数据库。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _setup_path() -> None:
    for candidate in (ROOT / "backend", ROOT / "scripts"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


_setup_path()

from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal, probe_database  # noqa: E402
from app.models.redline import RedLineRule  # noqa: E402
from app.models.threshold import RegulatoryThreshold  # noqa: E402
from app.services.rag.ingest import PLACEHOLDER_PREFIX  # noqa: E402
from app.services.rules.builtin_redlines import as_rule_rows  # noqa: E402
from app.services.rules.thresholds import THRESHOLD_SPECS  # noqa: E402
from sqlalchemy import select  # noqa: E402


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")

#: 逐字段比对这几项就够：它们全部会改变判定结果，且全部可能被库内旧值卡住
RULE_FIELDS = (
    "name",
    "severity",
    "keyword_patterns",
    "metric_key",
    "metric_operator",
    "threshold_value",
    "secondary_threshold",
    "threshold_source",
    "applies_to",
    "hard_block",
    "basis_refs",
    "priority",
    "is_active",
)


#: 提示行的前缀：这类行需要人工看一眼，但不算"漂移"，不参与退出码
INFO = "ℹ "


def _drift_only(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.startswith(INFO)]


async def audit_rules(session: Any, *, fix: bool) -> list[str]:
    """比对 `red_line_rules` 表与 `builtin_redlines.py` 的定义。"""
    rows = (await session.execute(select(RedLineRule))).scalars().all()
    by_code = {row.code: row for row in rows}
    drift: list[str] = []

    for expected in as_rule_rows():
        code = str(expected["code"])
        row = by_code.get(code)
        if row is None:
            drift.append(f"{code}: 库内没有这条红线（内置定义有），线上永远不会判它")
            continue
        for field in RULE_FIELDS:
            if field not in expected:
                continue
            want = expected[field]
            got = getattr(row, field, None)
            if isinstance(want, (list, dict)) and isinstance(got, (list, dict, type(None))):
                if json.dumps(got, sort_keys=True, ensure_ascii=False) != json.dumps(
                    want, sort_keys=True, ensure_ascii=False
                ):
                    drift.append(
                        f"{code}.{field}: 库内={_short(got)} 代码={_short(want)}"
                        + ("" if not fix else " → 已按代码库同步")
                    )
                    if fix:
                        setattr(row, field, want)
            elif got != want:
                drift.append(
                    f"{code}.{field}: 库内={_short(got)} 代码={_short(want)}"
                    + ("" if not fix else " → 已按代码库同步")
                )
                if fix:
                    setattr(row, field, want)

    extra = sorted(set(by_code) - {str(row["code"]) for row in as_rule_rows()})
    for code in extra:
        drift.append(f"{code}: 只在库内存在（可能是法务自建或历史残留），本次不判漂移")
    if fix and drift:
        await session.commit()
    return drift


async def audit_thresholds(session: Any, *, fix: bool) -> list[str]:
    """比对阈值表与 Settings/.env，并检查 LPR 是否过了公布周期。

    只报"会被读到的键"：`ALLOWED_KEYS` 之外的孤儿键不影响判定，
    但表里有孤儿键说明有人手工插过东西，所以单独列一行提示。
    """
    settings = get_settings()
    rows = (await session.execute(select(RegulatoryThreshold))).scalars().all()
    by_key = {row.key: row for row in rows}
    drift: list[str] = []

    for key, spec in THRESHOLD_SPECS.items():
        attr = str(spec.get("attr") or key)
        default = getattr(settings, attr, None)
        row = by_key.get(key)
        if row is None:
            drift.append(f"阈值 {key}: 库内未登记，运行时退回 {attr}={default}")
            continue
        if default is not None and abs(_as_float(row.value) - _as_float(default)) > 1e-9:
            hint = (
                "（THRESHOLDS_FROM_DB=true，生效的是库内值；改 .env 或改代码默认值都不会有任何效果）"
                if settings.thresholds_from_db
                else "（THRESHOLDS_FROM_DB=false，生效的是 .env/Settings）"
            )
            drift.append(
                f"阈值 {key}: 库内={row.value} 代码/环境={default} {hint}"
                + ("" if not fix else " → 已按代码库同步")
            )
            if fix:
                row.value = float(default)
                row.updated_by = "audit_config_drift"

    orphans = sorted(set(by_key) - set(THRESHOLD_SPECS))
    for key in orphans:
        drift.append(f"阈值 {key}: 只在库内存在，不在 THRESHOLD_SPECS 清单里（孤儿键，永远不会被读到）")

    lpr = by_key.get("lpr_one_year_rate")
    if lpr is not None:
        effective_on = lpr.effective_on
        age_days = (date.today() - effective_on).days if effective_on else None
        # 这行是提示不是漂移：LPR 可以连续几个月不变，"久未更新"本身不是错误，
        # 把它算进漂移会让审计的退出码失去意义（CI 会天天红）
        drift.append(
            f"{INFO} LPR 现值 {lpr.value} → 4×LPR = {lpr.value * 4:.4f}"
            f"（更新人 {lpr.updated_by}，生效日 {effective_on}"
            + (f"，距今 {age_days} 天" if age_days is not None else "")
            + "）；每月 20 日前后需人工核对是否变了"
        )

    if fix and any(not line.startswith(INFO) for line in drift):
        await session.commit()
    return drift


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
    return text if len(text) <= 90 else text[:87] + "…"


async def audit_knowledge(session: Any) -> list[str]:
    """知识库完整度：它决定"引用已核验率"的天花板，也决定 GEN-06 能不能算。

    原文没导入时所有引用都判未核验并强制转人工——这是设计行为，不是模型幻觉。
    但报告必须把这个比例说出来，否则"引用已核验率 5%"会被当成模型质量差。
    """
    from app.models.knowledge import LegalProvision

    provisions = (await session.execute(select(LegalProvision))).scalars().all()
    if not provisions:
        return ["legal_provisions 为空：引用核验一律不通过，检索与引用类指标全部不可测"]

    placeholders = [row for row in provisions if (row.content or "").startswith(PLACEHOLDER_PREFIX)]
    lines = [
        f"legal_provisions 共 {len(provisions)} 行，占位 {len(placeholders)} 行"
        f"（{len(placeholders) / len(provisions):.0%}）；"
        f"有权威原文 {len(provisions) - len(placeholders)} 行"
    ]
    if placeholders:
        names = sorted({row.law_name for row in placeholders})
        lines.append(
            "占位涉及的法规：" + "、".join(f"《{name}》" for name in names[:10])
            + ("…" if len(names) > 10 else "")
        )
        lines.append(
            "这些法规的引用只能标『未核验』并转人工：属知识库欠账（导入 testdata/legal 的原文即可解），"
            "不计入模型幻觉率"
        )

    # 法条引用是否到"条号级"：只到整部法规（article_no='全文'）的红线，
    # 核验只认那条全文哨兵行，逐条导入并不会让它们变成已核验
    law_level = [row for row in provisions if row.article_no == "全文"]
    lines.append(f"以『全文』级登记的条文 {len(law_level)} 行（哨兵行必须整篇导入才能翻成已核验）")
    return lines


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="配置漂移审计")
    parser.add_argument("--only", choices=("rules", "thresholds", "knowledge"), default="")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="把库内值按代码库口径同步（写库操作，确认后再用）",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if await probe_database() != "connected":
        print("数据库不可达，无法审计。")
        return 2

    found: list[str] = []
    async with SessionLocal() as session:
        if args.only in ("", "rules"):
            print("\n== 红线定义漂移（red_line_rules vs builtin_redlines.py）==")
            drift = await audit_rules(session, fix=args.fix)
            _print(drift)
            found += drift
        if args.only in ("", "thresholds"):
            print("\n== 阈值口径漂移（regulatory_thresholds vs Settings/.env）==")
            drift = await audit_thresholds(session, fix=args.fix)
            _print(drift)
            found += drift
        if args.only in ("", "knowledge"):
            print("\n== 知识库完整度（决定引用核验率天花板）==")
            drift = [INFO + line for line in await audit_knowledge(session)]
            _print(drift)

    real = _drift_only(found)
    print(f"\n{'已同步' if args.fix else '审计完成'}：{len(real)} 项真实差异，"
          f"{len(found) - len(real)} 条人工核对提示")
    if real and not args.fix:
        print("带 --fix 可按代码库口径同步库内值（会写库，请先确认哪边是对的）")
        return 1
    return 0


def _print(lines: list[str]) -> None:
    if not lines:
        print("  无差异")
        return
    for line in lines:
        print(f"  - {line}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
