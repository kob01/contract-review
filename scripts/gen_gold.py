"""从规则引擎的实际输出生成金标准（gold set），并点名仍需人工补的字段。

为什么自动生成而不是全手写标注：
22 条红线的"应触发/不应触发"与命中偏移，规则引擎本身就是确定性来源——判定可复现、
可解释。把这部分抄进金标准不引入标注噪声，还省掉律师的机械劳动。真正需要人做的是三件事，
本脚本一律留空并在 README 里点名：模型通道才能发现的模糊结论、期望严重度是否该改判、
边界与跨种类误用样本。

运行（在 `backend/` 目录下；脚本依赖 `app` 包与 `tests.fixtures`）：

    uv run python ../scripts/gen_gold.py            # 生成/覆盖 testdata/gold/*.json
    uv run python ../scripts/gen_gold.py --check    # 只校验金标准与引擎实际输出是否仍一致

`--check` 是回归守卫：改正则或调阈值导致命中集合变化时，它先报"金标准与引擎不一致"，
而不是让评测报告安静地端出一个新数字（那种数字没人敢信）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GOLD_DIR = ROOT / "testdata" / "gold"
CONTRACT_DIR = ROOT / "testdata" / "contracts"

#: 需要人工补的部分，报告里逐条点名，避免"生成了就等于标注完了"
NEEDS_HUMAN = (
    "riskLevel：脚本抄的是红线定义值，法务可以改判（改了就以此文件为准）",
    "仅模型通道能发现的模糊结论：本集只覆盖规则可判定的部分",
    "边界扫描样本：每个阈值 ±1pp 带内至少 8 个点（现在每阈值只有 1 个正例）",
    "跨合同种类误用样本：例如把持牌机构 24% 口径套到民间借贷",
)


def _setup_path() -> None:
    """脚本要在 `backend/` 里跑，但被 `scripts/` 引用时需要自己补导入路径。"""
    for candidate in (ROOT / "backend", ROOT / "scripts"):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


_setup_path()

from app.core.config import settings  # noqa: E402
from app.models.redline import RedLineRule  # noqa: E402
from app.services.parser.extractor import ExtractedDocument  # noqa: E402
from app.services.parser.segmenter import segmentize  # noqa: E402
from app.services.rules.builtin_redlines import as_rule_rows  # noqa: E402
from app.services.rules.engine import RuleEngine  # noqa: E402
from app.services.rules.metrics import extract_metrics  # noqa: E402
from tests.fixtures import build_document  # noqa: E402

RULE_ROWS: list[dict[str, Any]] = as_rule_rows()
ALL_CODES: set[str] = {str(row["code"]) for row in RULE_ROWS}


def _refs_by_code() -> dict[str, list[dict[str, Any]]]:
    """每条红线声明的法规依据（法规名 + 条号）。

    条号为 None 时按 `ingest.LAW_LEVEL_ARTICLE_NO` 的哨兵口径写成"全文"：
    核验对这类引用只认那条"全文"记录，金标准若写空值，两边永远对不上。
    """
    mapping: dict[str, list[dict[str, Any]]] = {}
    for row in RULE_ROWS:
        refs = []
        for ref in row.get("basis_refs") or []:
            law_name = str(ref.get("law_name") or "").strip()
            if law_name:
                refs.append({"lawName": law_name, "articleNo": ref.get("article_no") or "全文"})
        mapping[str(row["code"])] = refs
    return mapping


def _severity_by_code() -> dict[str, str]:
    return {str(row["code"]): str(row.get("severity") or "") for row in RULE_ROWS}


def build_engine() -> RuleEngine:
    rows = [dict(row) for row in RULE_ROWS]
    for row in rows:
        row.setdefault("is_active", True)
    return RuleEngine([RedLineRule(**row) for row in rows], settings)


def build_gold_for_sample(
    filename: str,
    expected_kind: str,
    expected_hits: set[str],
    text: str,
    *,
    engine: RuleEngine,
    refs_by_code: dict[str, list[dict[str, Any]]],
    severity_by_code: dict[str, str],
) -> dict[str, Any]:
    """跑一遍真实解析与规则引擎，把命中情况摊成一条金标准记录。"""
    document: ExtractedDocument = build_document(text)
    segments = segmentize(document)
    metrics = extract_metrics(document, segments)
    evaluation = engine.evaluate(
        metrics, full_text=text, segments=segments, contract_kind=expected_kind
    )
    hit_by_code = {hit.rule_code: hit for hit in evaluation.hits}

    findings: list[dict[str, Any]] = []
    for code in sorted(expected_hits):
        hit = hit_by_code.get(code)
        entry: dict[str, Any] = {
            "ruleCode": code,
            "riskLevel": severity_by_code.get(code) or (hit.severity if hit else None),
            "clauseCategory": hit.category if hit else None,
            "refs": refs_by_code.get(code, []),
            # 规则可判定的条目不接受"只有模型说"：模型在无规则佐证时发明它，应算误报
            "acceptLlmOnly": False,
        }
        if hit is not None and hit.evidence is not None:
            entry["span"] = {
                "charStart": hit.evidence.char_start,
                "charEnd": hit.evidence.char_end,
                "excerptContains": hit.evidence.excerpt[:80],
            }
        else:
            # 派生值命中（如 max_annual_cost_rate 取最劣口径）没有单一原文可指。
            # 这是被测系统的真实缺陷，不是标注遗漏：如实记下来，
            # 评测才不会把"定位不到"算成"标注没标"而悄悄跳过
            entry["span"] = {"excerptContains": ""}
            entry["notes"] = "由派生指标命中，无单一原文区间；需人工指定应高亮的条款"
        findings.append(entry)

    must_retrieve: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for code in sorted(expected_hits):
        for ref in refs_by_code.get(code, []):
            key = (ref["lawName"], ref.get("articleNo"))
            if key not in seen:
                seen.add(key)
                must_retrieve.append(dict(ref))

    return {
        "caseId": Path(filename).stem,
        "fixture": f"testdata/contracts/{filename}",
        "contractKind": expected_kind,
        "expectNoFindings": not expected_hits,
        "findings": findings,
        # 负例样本：任何红线都不许出现。这是测误报率的唯一干净口径
        "forbiddenRuleCodes": sorted(ALL_CODES - expected_hits) if not expected_hits else [],
        "mustRetrieve": must_retrieve,
        "notes": "由 scripts/gen_gold.py 从规则引擎实际输出派生",
    }


def render_readme(cases: list[dict[str, Any]]) -> str:
    total_findings = sum(len(case["findings"]) for case in cases)
    unlocatable = sum(
        1 for case in cases for item in case["findings"] if not (item.get("span") or {}).get("charStart")
    )
    retrieval_refs = sorted(
        {ref["lawName"] for case in cases for ref in case["mustRetrieve"]},
    )
    lines = [
        "# 金标准集（gold set）",
        "",
        "由 `scripts/gen_gold.py` 从规则引擎实际输出派生，`scripts/eval.py` 读取。",
        "每条记录含：期望红线、期望严重度、期望原文区间、期望法规依据、",
        "禁止出现的红线（负例口径）、以及必须被检索召回的条文。",
        "",
        f"- 用例：{len(cases)} 份；期望结论：{total_findings} 条",
        f"- 其中无原文区间：{unlocatable} 条（派生指标命中，见下方缺陷）",
        f"- 需检索覆盖的法规：{len(retrieval_refs)} 部",
        "",
        "## 仍需人工标注",
        "",
    ]
    lines += [f"- {item}" for item in NEEDS_HUMAN]
    lines += [
        "",
        "## 已知缺陷（如实记录，不掩盖）",
        "",
        "- 派生指标命中的条目没有单一原文区间，评测会把这类条目当\"未标注 span\"，",
        "  而不是\"定位失败\"——GEN-12 的分母因此是诚实的。",
        "- `small_loan` 无法由 `infer_contract_kind` 推断，测小贷口径必须显式指定种类（RULE-04 已知系统性缺陷）。",
        "- 全部夹具为 `.txt`：PDF/DOCX 的抽取保真与页码归属（PAR-01/05/06）不在本集覆盖范围内。",
        "- `forbiddenRuleCodes` 对 POS 用例留空：误报率目前只能从 NEG 用例与跨种类样本算。",
        "",
        "## 覆盖到的法规",
        "",
    ]
    lines += [f"- 《{name}》" for name in retrieval_refs]
    lines.append("")
    return "\n".join(lines)


def main(check_only: bool) -> int:
    import gen_test_contracts as fixtures

    engine = build_engine()
    refs_by_code = _refs_by_code()
    severity_by_code = _severity_by_code()

    problems: list[str] = []
    cases: list[dict[str, Any]] = []
    GOLD_DIR.mkdir(parents=True, exist_ok=True)

    for filename, expected_kind, expected_hits, text in fixtures.SAMPLES:
        path = CONTRACT_DIR / filename
        if path.exists():
            text = path.read_text(encoding="utf-8")
        elif check_only:
            problems.append(f"{filename}: 夹具不存在，先跑 gen_test_contracts.py")
            continue

        payload = build_gold_for_sample(
            filename,
            expected_kind,
            set(expected_hits),
            text,
            engine=engine,
            refs_by_code=refs_by_code,
            severity_by_code=severity_by_code,
        )
        cases.append(payload)
        target = GOLD_DIR / f"{Path(filename).stem}.json"

        if check_only:
            if not target.exists():
                problems.append(f"{target.name}: 金标准不存在，跑 gen_gold.py 生成")
                continue
            existing = json.loads(target.read_text(encoding="utf-8"))
            old = {item.get("ruleCode") for item in existing.get("findings") or []}
            new = {item.get("ruleCode") for item in payload["findings"]}
            if old != new:
                problems.append(
                    f"{target.name}: 金标准期望 {sorted(old)}，引擎实际 {sorted(new)} —— "
                    "改了正则或阈值，先确认哪个才是对的"
                )
            old_kind = existing.get("contractKind")
            if old_kind != payload["contractKind"]:
                problems.append(
                    f"{target.name}: 种类期望 {old_kind}，实际推断 {payload['contractKind']}"
                )
            continue

        if not target.exists():
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        else:
            # 已存在的金标准可能已被人工改过：默认不覆盖人工标注，只补缺失文件。
            # 要强制重生成就先删掉 testdata/gold/ 再跑
            problems.append(f"{target.name}: 已存在，保留现状（如需重生成请先删除）")

    if not check_only:
        (GOLD_DIR / "_README.md").write_text(render_readme(cases), encoding="utf-8")
        print(f"生成 {len(cases)} 份金标准 → {GOLD_DIR}")
        print("已存在而未被覆盖的文件数：见下方列表（人工标注优先保留）")
        for item in problems:
            if "已存在" in item:
                print(f"  · {item}")
        problems = [item for item in problems if "已存在" not in item]

    if problems:
        print(f"\n问题 {len(problems)} 项：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n金标准与引擎实际输出一致。" if check_only else "\n生成完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
