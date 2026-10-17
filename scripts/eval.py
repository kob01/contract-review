"""评测跑分入口：金标准 → 全链路审查 → 指标 → 报告 → 与基线 diff。

    # 便宜的日常回归：只跑规则通道，不调模型、不花 token
    cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=. uv run python ../scripts/eval.py --rule-only

    # 完整评测（真实模型 + 真实检索），产出基线
    PYTHONIOENCODING=utf-8 PYTHONPATH=. uv run python ../scripts/eval.py --out ../eval/reports

    # 版本对比：与已有基线比，回归就非零退出（CI 用这个）
    PYTHONIOENCODING=utf-8 PYTHONPATH=. uv run python ../scripts/eval.py \
        --rule-only --baseline ../eval/reports/baseline.json

退出码：
  0 = 跑通且（给了基线时）无回归
  1 = 有回归 / 金标准校验失败（--strict）/ 计数不变式不自相矛盾检查未过
  2 = 无法评测（金标准目录缺失、依赖不可达导致全部跳过）

`--rule-only` 不是"降级模式"，而是两类问题的分工：规则通道的判定是可复现的，
它回归了说明正则或阈值被改坏；模型通道受供应商与思考档位影响，波动大，
适合定时跑并和基线比，不适合每次提交都跑（既慢又花钱）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
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
from app.schemas.review import ReviewRunRequest  # noqa: E402
from app.services.eval.gold import GoldCase, load_gold_set  # noqa: E402
from app.services.eval.report import diff_baseline, evaluate, render_markdown  # noqa: E402
from app.services.eval.runner import run_case  # noqa: E402
from app.services.llm.factory import (  # noqa: E402
    JudgeSlot,
    describe_judge_backend,
    describe_llm_backend,
)
from app.services.rules.seed import load_active_rules  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合同审查 Agent 评测跑分")
    parser.add_argument("--gold", default=str(ROOT / "testdata" / "gold"), help="金标准目录")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 例（0 = 全部）")
    parser.add_argument("--ids", default="", help="逗号分隔的用例 id 白名单")
    parser.add_argument("--rule-only", action="store_true", help="只跑规则通道，不调模型")
    parser.add_argument(
        "--respect-gold-rule-only",
        action="store_true",
        help="按金标准每条记录里的 ruleOnly 决定（默认全部走完整链路）",
    )
    parser.add_argument("--repeats", type=int, default=1, help="每例重跑次数（>=2 才算一致性）")
    parser.add_argument("--out", default=str(ROOT / "eval" / "reports"), help="报告输出目录")
    parser.add_argument("--baseline", default="", help="基线 JSON 路径，用于回归对比")
    parser.add_argument("--set-baseline", action="store_true", help="把本次结果写为基线")
    parser.add_argument("--strict", action="store_true", help="金标准校验失败直接报错")
    parser.add_argument("--top-k", type=int, default=0, help="覆盖检索条数（0 = 用配置值）")
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="先建表并写入内置红线与阈值默认值（CI 在全新数据库上跑评测时用）",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()

    state = await probe_database()
    if state != "connected":
        print(f"数据库不可达（{state}），无法评测。先起 `docker compose up -d db`。")
        return 2

    gold_dir = Path(args.gold)
    if not gold_dir.exists():
        print(f"金标准目录不存在：{gold_dir}\n先跑：uv run python ../scripts/gen_gold.py")
        return 2

    if args.bootstrap:
        # 全新数据库里红线表是空的，评测会报"检出召回 0"——那不是模型差，是没数据可读。
        # 与线上启动同一套播种函数，保证 CI 环境和真实环境的库内口径一致
        from app.db.session import init_models
        from app.services.rules.seed import ensure_builtin_rules
        from app.services.rules.thresholds import ensure_default_thresholds

        await init_models()
        async with SessionLocal() as session:
            created_rules = await ensure_builtin_rules(session)
            created_thresholds = await ensure_default_thresholds(session, settings)
        print(f"播种：红线新增 {created_rules} 条，阈值新增 {created_thresholds} 条")

    gold = load_gold_set(gold_dir, strict=args.strict)
    if gold.problems and not args.strict:
        print(f"金标准有 {len(gold.problems)} 处问题（继续跑，但报告会点名）：")
        for item in gold.problems[:10]:
            print(f"  - {item}")

    cases = list(gold.cases)
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        cases = [case for case in cases if case.case_id in wanted or case.case_id.split("_")[0] in wanted]
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        print("没有可跑的用例。")
        return 2
    gold.cases = cases

    async with SessionLocal() as session:
        rules = await load_active_rules(session)
    hard_block_codes = {rule.code for rule in rules if getattr(rule, "hard_block", False)}
    # 打印"实际会用的后端"而不是配置字面值：LLM_PROVIDER=auto 时没密钥就是 Mock，
    # 报告显示 glm 而其实在跑 Mock，是最容易骗过自己的一种错误
    review_backend = describe_llm_backend(settings)
    judge_backend = describe_judge_backend(settings)
    print(
        f"跑 {len(cases)} 例 × {max(1, args.repeats)} 次；"
        f"通道={'规则 only' if args.rule_only else '规则 + LLM/RAG + 复核'}；"
        f"主审={review_backend[0]}/{review_backend[1]}（思考 {review_backend[2]}）；"
        f"复核={judge_backend.mode}/{judge_backend.model or '—'}"
        f"{'（独立）' if judge_backend.independent else ''}"
        f"{'（' + judge_backend.warning + '）' if judge_backend.warning else ''}；"
        f"硬阻断红线 {len(hard_block_codes)} 条"
    )

    async def run_one(case: GoldCase) -> Any:
        rule_only = args.rule_only or (args.respect_gold_rule_only and case.rule_only)
        request = ReviewRunRequest(
            rule_only=rule_only,
            top_k=args.top_k or None,
            focus_categories=[],
        )
        return await run_case(
            case_id=case.case_id,
            path=case.fixture,
            settings=settings,
            request=request,
            contract_kind=case.contract_kind,
            # 规则-only 时连复核一起关掉：这个开关的语义是"不产生模型调用与费用"
            judge_slot=JudgeSlot() if rule_only else None,
        )

    started = datetime.now(UTC)
    async with SessionLocal() as session:
        report = await evaluate(
            gold,
            session=session,
            run_one=run_one,
            hard_block_codes=hard_block_codes,
            threshold=settings.confidence_routing_threshold,
            repeats=max(1, args.repeats),
        )
    report["meta"]["startedAt"] = started.isoformat(timespec="seconds")
    report["meta"]["mode"] = "rule-only" if args.rule_only else "full"
    # 记"实际生效的后端"而不是配置字面值：LLM_PROVIDER=mock 时报告上仍写 glm，
    # 这份报告就会被当成真实模型的成绩单——门禁和基线全都建立在它之上
    report["meta"]["model"] = {
        "review": review_backend[1],
        "reviewProvider": review_backend[0],
        "reviewReasoning": review_backend[2],
        "judge": judge_backend.model or "—",
        "judgeMode": judge_backend.mode,
        "judgeIndependent": judge_backend.independent,
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    (out_dir / f"report-{stamp}.json").write_text(payload + "\n", encoding="utf-8")
    (out_dir / f"report-{stamp}.md").write_text(render_markdown(report), encoding="utf-8")
    (out_dir / "latest.json").write_text(payload + "\n", encoding="utf-8")

    metrics = report["metrics"]
    detection = metrics["detection"]["detection"]
    hard_block = metrics["detection"]["hardBlock"]
    print(
        f"\n检出 micro 召回={detection['micro']['recall']} F2={detection['micro']['f2']} "
        f"macro F1={detection['macro']['f1']}；硬阻断漏报={hard_block['missedCount']}；"
        f"引用已核验率={metrics['citation']['citation']['verifiedRate']}"
    )
    print(f"报告：{out_dir / f'report-{stamp}.md'}")

    exit_code = 0

    # 计数不变式是"系统自己跟自己矛盾"，任何一条都该拦下来
    invariants = metrics["invariants"]
    if invariants["checked"] and not invariants["clean"]:
        print(f"\n❌ 报告一致性检查未过（{len(invariants['problems'])} 处）：")
        for item in invariants["problems"][:10]:
            print(f"  - {item['caseId']}：{item['issue']}")
        exit_code = 1

    if args.set_baseline:
        (out_dir / "baseline.json").write_text(payload + "\n", encoding="utf-8")
        print(f"已写入基线：{out_dir / 'baseline.json'}")

    if args.baseline:
        baseline_path = Path(args.baseline)
        if not baseline_path.exists():
            print(f"\n基线文件不存在：{baseline_path}")
            return 2
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        diffs = diff_baseline(report, baseline)
        print("\n与基线对比：")
        for item in diffs:
            mark = {"improved": "↑", "regressed": "↓", "within-noise": "=", "same": "=", "missing": "?"}[
                item["verdict"]
            ]
            print(
                f"  {mark} {item['metric']}: {item.get('before')} → {item.get('after')}"
                + (f"（Δ{item['delta']}）" if "delta" in item else "")
            )
        if any(item["verdict"] == "regressed" for item in diffs):
            print("\n❌ 存在超出噪声阈值的回归")
            exit_code = 1

    if report["meta"]["measured"] == 0:
        print("\n❌ 没有任何用例跑通，本次不构成评测结论")
        exit_code = 2
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
