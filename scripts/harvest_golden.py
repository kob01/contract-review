"""复核结论回流：把法务的人工判定变成金标准候选（RTR-12 / DAT-08）。

一条硬规则：**回流只产出候选，不直接改写 `testdata/gold/`**。
如果用系统的输出自动更新金标准，再用金标准去评系统，分数会自我收敛到满分——
这是评测体系最容易自我欺骗的地方，而它看起来完全像"数据飞轮"。
所以候选文件写到 `testdata/gold/candidates/`（加载器跳过 `_` 前缀与子目录），
由人确认后再手工移入正式目录。

    cd backend && PYTHONIOENCODING=utf-8 PYTHONPATH=. uv run python ../scripts/harvest_golden.py
    ... harvest_golden.py --limit 200 --min-confidence 0.6

输出：
- `testdata/gold/candidates/*.json`：每条已复核结论对应的期望/禁止红线
- 终端统计：复核覆盖率、各 verdict 分布、以及"还差多少才能支撑 GEN-04 的严重度标注"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _setup_path() -> None:
    for candidate in (ROOT / "backend", ROOT / "scripts"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


_setup_path()

from app.db.session import SessionLocal  # noqa: E402
from app.models.enums import HumanReviewStatus  # noqa: E402
from app.models.review import ContractReview, ReviewFinding  # noqa: E402
from sqlalchemy import select  # noqa: E402

CANDIDATE_DIR = ROOT / "testdata" / "gold" / "candidates"

#: 人工判定 → 金标准动作。
#: adjusted 表示"结论成立但等级要改"，所以它既进期望集合又带严重度修正
VERDICT_MAP = {
    HumanReviewStatus.APPROVED.value: "expect",
    HumanReviewStatus.ADJUSTED.value: "expect",
    HumanReviewStatus.REJECTED.value: "forbid",
}


async def collect(*, limit: int, min_confidence: float) -> dict[str, list[dict[str, Any]]]:
    """按"合同 + 审查"聚合已复核结论。

    `min_confidence` 只用于收窄候选规模：极低置信却被人确认成立的结论同样值得回流，
    所以默认 0（全收），调高它是在挑选"更有把握的样本"而不是在过滤噪声。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    async with SessionLocal() as session:
        rows = await session.execute(
            select(ReviewFinding, ContractReview)
            .join(ContractReview, ReviewFinding.review_id == ContractReview.id)
            .where(
                ReviewFinding.human_review_status.in_(
                    [
                        HumanReviewStatus.APPROVED.value,
                        HumanReviewStatus.ADJUSTED.value,
                        HumanReviewStatus.REJECTED.value,
                    ]
                )
            )
            .order_by(ReviewFinding.review_id, ReviewFinding.order_index)
            .limit(limit)
        )
        for finding, review in rows.all():
            if float(finding.confidence or 0.0) < min_confidence:
                continue
            grouped.setdefault(str(review.id), []).append(
                {
                    "reviewId": str(review.id),
                    "contractId": str(review.contract_id),
                    "ruleCode": finding.rule_code,
                    "riskLevel": finding.adjusted_risk_level or finding.risk_level,
                    "source": finding.source,
                    "verdict": finding.human_review_status,
                    "confidence": finding.confidence,
                    "verificationStatus": finding.verification_status,
                    "locateMethod": finding.locate_method,
                    "excerpt": (finding.excerpt or "")[:160],
                    "span": {
                        "charStart": finding.char_start,
                        "charEnd": finding.char_end,
                        "excerptContains": (finding.excerpt or "")[:60],
                    },
                    "reviewer": finding.human_reviewer_id,
                    "comment": (finding.human_comment or "")[:300],
                }
            )
    return grouped


def build_candidate(review_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    expected = sorted(
        {
            str(item["ruleCode"])
            for item in items
            if item["ruleCode"] and VERDICT_MAP.get(item["verdict"]) == "expect"
        }
    )
    forbidden = sorted(
        {
            str(item["ruleCode"])
            for item in items
            if item["ruleCode"] and VERDICT_MAP.get(item["verdict"]) == "forbid"
        }
    )
    findings = []
    for item in items:
        if not item["ruleCode"] or VERDICT_MAP.get(item["verdict"]) != "expect":
            continue
        findings.append(
            {
                "ruleCode": item["ruleCode"],
                "riskLevel": item["riskLevel"],
                "acceptLlmOnly": item["source"] == "llm",
                "span": item["span"],
                "notes": f"复核人 {item['reviewer']}：{item['comment']}"[:200],
                "labeledBy": "human-review",
            }
        )
    return {
        "caseId": f"HR-{review_id[:8]}",
        "fixture": f"TODO 指向 review {review_id} 对应的合同文件",
        "contractKind": None,
        "expectNoFindings": not expected,
        "findings": findings,
        "forbiddenRuleCodes": forbidden,
        "mustRetrieve": [],
        "tags": ["harvested", "needs-human-confirm"],
        "notes": "由 scripts/harvest_golden.py 从人工复核结论派生，需法务确认后再移入 testdata/gold/",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="复核结论回流")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true", help="只统计不写候选文件")
    args = parser.parse_args(argv)

    grouped = asyncio.run(collect(limit=args.limit, min_confidence=args.min_confidence))
    if not grouped:
        print("还没有任何已复核结论（approved/adjusted/rejected）。")
        print("数据飞轮的起点是复核队列被真正处理过：先在界面完成若干条复核，再回来跑本脚本")
        return 0

    verdicts: Counter[str] = Counter()
    for items in grouped.values():
        for item in items:
            verdicts[str(item["verdict"])] += 1

    reviewed = sum(verdicts.values())
    rejected = verdicts.get("rejected", 0)
    adjusted = verdicts.get("adjusted", 0)
    approved = verdicts.get("approved", 0)
    print(f"已复核结论 {reviewed} 条，覆盖 {len(grouped)} 次审查")
    print(f"  确认成立 {approved}｜需改等级 {adjusted}｜误报 {rejected}")
    print(f"  系统自动精确率（RTR-09，approved+adjusted / 已复核）：{(reviewed - rejected) / reviewed:.2%}")
    print(f"  等级需修正比例（RTR-10 严重度校准差）：{adjusted / reviewed:.2%}")

    if args.dry_run:
        print("--dry-run：未写候选文件")
        return 0

    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    written = 0
    for review_id, items in grouped.items():
        candidate = build_candidate(review_id, items)
        target = CANDIDATE_DIR / f"HR-{review_id[:8]}.json"
        if target.exists():
            continue
        target.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
    print(f"\n写入候选 {written} 份 → {CANDIDATE_DIR}")
    print(f"目录内累计 {len(list(CANDIDATE_DIR.glob('*.json')))} 份待确认")
    print("确认无误后手工移到 testdata/gold/ 并补上 TODO 的 fixture 路径")
    print("提醒：候选不进正式目录就不参与评分——自动把系统输出写成金标准，等于让它给自己打分")
    return 0


if __name__ == "__main__":
    sys.exit(main())
