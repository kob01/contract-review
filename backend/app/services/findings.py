"""双通道结论的合并、交叉校验与置信度路由。

刻意写成不依赖数据库的纯函数：交叉校验和路由是这套系统里最容易出事故的地方
（该转人工的没转、规则被模型推翻），必须能脱离模型与 PG 单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.enums import (
    CrossCheckStatus,
    FindingSource,
    HumanReviewReason,
    HumanReviewStatus,
    RiskLevel,
)


@dataclass(slots=True)
class CandidateFinding:
    """一条待落库的结论，字段与 `ReviewFinding` 对齐。"""

    title: str
    risk_level: str
    clause_category: str
    analysis: str
    suggestion: str
    excerpt: str
    source: str
    confidence: float
    cross_check_status: str
    rewritten_text: str | None = None
    rule_code: str | None = None
    metric_snapshot: dict = field(default_factory=dict)
    segment_index: int | None = None
    page_no: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    citations: list[dict] = field(default_factory=list)
    #: 同类违规的监管罚单 id（类比参考，不是法条依据），仅在 RAG_USE_CASES 打开时非空
    similar_case_ids: list[str] = field(default_factory=list)
    llm_confidence: float = 0.0
    rule_confidence: float = 0.0
    hard_block: bool = False
    located: bool = True
    #: 定位手段（exact/normalized/segment_fallback/evidence/failed），落 `ReviewFinding.locate_method`
    locate_method: str = ""
    human_review_status: str = HumanReviewStatus.NOT_REQUIRED.value
    human_review_reason: str | None = None
    #: 复核裁定快照，由 `apply_judge_verdicts` 写入
    judge_verdict: dict = field(default_factory=dict)

    @property
    def has_unverified_citation(self) -> bool:
        return any(not citation.get("verified", False) for citation in self.citations)

    @property
    def risk_enum(self) -> RiskLevel:
        return RiskLevel(self.risk_level)


def _overlaps(rule_item: CandidateFinding, llm_item: CandidateFinding) -> bool:
    """判断两条结论是否指向同一处条款问题。"""
    if rule_item.rule_code and llm_item.rule_code and rule_item.rule_code == llm_item.rule_code:
        return True

    if (
        rule_item.segment_index is not None
        and llm_item.segment_index is not None
        and rule_item.segment_index == llm_item.segment_index
        and rule_item.clause_category == llm_item.clause_category
    ):
        return True

    # 字符区间相交（模型引用与规则引用重叠 ≥ 30%）
    if None not in (
        rule_item.char_start,
        rule_item.char_end,
        llm_item.char_start,
        llm_item.char_end,
    ):
        overlap = min(rule_item.char_end, llm_item.char_end) - max(  # type: ignore[type-var]
            rule_item.char_start, llm_item.char_start  # type: ignore[type-var]
        )
        shorter = min(
            rule_item.char_end - rule_item.char_start,  # type: ignore[operator]
            llm_item.char_end - llm_item.char_start,  # type: ignore[operator]
        )
        if shorter > 0 and overlap / shorter >= 0.3:
            return True

    return False


def merge_and_cross_check(
    rule_items: list[CandidateFinding],
    llm_items: list[CandidateFinding],
) -> tuple[list[CandidateFinding], int]:
    """合并两条通道的结论。

    冲突时以规则引擎为准（包括风险等级），但保留模型的分析文本，
    并把冲突计入 `conflict_count`——冲突率是模型质量的日常监控指标。

    返回 `(合并后的结论, 冲突条数)`。
    """
    merged: list[CandidateFinding] = []
    used_llm: set[int] = set()
    conflicts = 0

    for rule_item in rule_items:
        matched_index: int | None = None
        for index, llm_item in enumerate(llm_items):
            if index in used_llm:
                continue
            if _overlaps(rule_item, llm_item):
                matched_index = index
                break

        if matched_index is None:
            rule_item.source = FindingSource.RULE.value
            rule_item.cross_check_status = CrossCheckStatus.RULE_ONLY.value
            rule_item.rule_confidence = rule_item.confidence
            merged.append(rule_item)
            continue

        llm_item = llm_items[matched_index]
        used_llm.add(matched_index)

        combined = rule_item
        combined.source = FindingSource.BOTH.value
        combined.llm_confidence = llm_item.confidence
        combined.rule_confidence = rule_item.confidence

        if rule_item.risk_level == llm_item.risk_level:
            combined.cross_check_status = CrossCheckStatus.CONSISTENT.value
            # 双通道一致，置信度小幅上调并封顶
            combined.confidence = min(1.0, max(rule_item.confidence, llm_item.confidence) + 0.05)
        else:
            combined.cross_check_status = CrossCheckStatus.RULE_OVERRIDES.value
            combined.confidence = max(rule_item.confidence, llm_item.confidence)
            combined.risk_level = rule_item.risk_level  # 规则为准
            combined.analysis = (
                f"{rule_item.analysis}\n[模型不同意见] {llm_item.analysis}"
                f"（等级 {llm_item.risk_level}，已按规则引擎口径校正）"
            )
            conflicts += 1

        if llm_item.suggestion and not combined.suggestion:
            combined.suggestion = llm_item.suggestion
        if llm_item.rewritten_text and not combined.rewritten_text:
            combined.rewritten_text = llm_item.rewritten_text
        if llm_item.citations and not combined.citations:
            combined.citations = llm_item.citations

        merged.append(combined)

    for index, llm_item in enumerate(llm_items):
        if index in used_llm:
            continue
        llm_item.source = FindingSource.LLM.value
        llm_item.cross_check_status = CrossCheckStatus.LLM_ONLY.value
        llm_item.llm_confidence = llm_item.confidence
        merged.append(llm_item)

    return merged, conflicts


#: 一个结论只会因最主要的原因进复核队列，按此优先级取第一个命中
_REASON_PRIORITY = (
    HumanReviewReason.RULE_LLM_CONFLICT,
    HumanReviewReason.UNVERIFIED_CITATION,
    HumanReviewReason.METRIC_MISSING,
    HumanReviewReason.JUDGE_DISPUTED,
    HumanReviewReason.LOW_CONFIDENCE,
    HumanReviewReason.HIGH_RISK_MANDATORY,
)


def route_reason(
    item: CandidateFinding,
    *,
    threshold: float,
    mandatory_high_risk: bool,
) -> HumanReviewReason | None:
    """判定是否需要人工复核，以及最主要的原因。

    规则引擎硬命中的结论本身可复现，唯一能让它进队列的理由是
    "引用依据没核验"或"高风险强制确认"；置信度阈值只约束模型通道。
    """
    reasons: set[HumanReviewReason] = set()

    if item.cross_check_status == CrossCheckStatus.RULE_OVERRIDES.value:
        reasons.add(HumanReviewReason.RULE_LLM_CONFLICT)

    if item.has_unverified_citation:
        reasons.add(HumanReviewReason.UNVERIFIED_CITATION)

    if not item.located:
        # 原文定位失败会让前端高亮落空，必须人工确认引用是否成立
        reasons.add(HumanReviewReason.METRIC_MISSING)

    pure_llm = item.source in (FindingSource.LLM.value,)
    if pure_llm and item.confidence < threshold:
        reasons.add(HumanReviewReason.LOW_CONFIDENCE)

    if mandatory_high_risk and item.risk_level == RiskLevel.HIGH.value:
        reasons.add(HumanReviewReason.HIGH_RISK_MANDATORY)

    for reason in _REASON_PRIORITY:
        if reason in reasons:
            return reason
    return None


def apply_routing(
    items: list[CandidateFinding],
    *,
    threshold: float,
    mandatory_high_risk: bool,
) -> dict[str, list[tuple[CandidateFinding, HumanReviewReason]]]:
    """按路由结果分组，同时写回复核状态。"""
    buckets: dict[str, list[tuple[CandidateFinding, HumanReviewReason]]] = {}

    for item in items:
        reason = route_reason(
            item, threshold=threshold, mandatory_high_risk=mandatory_high_risk
        )
        if reason is None:
            item.human_review_status = HumanReviewStatus.NOT_REQUIRED.value
            item.human_review_reason = None
            continue
        item.human_review_status = HumanReviewStatus.PENDING.value
        item.human_review_reason = reason.value
        buckets.setdefault(item.rule_code or "llm", []).append((item, reason))

    return buckets


def apply_judge_verdicts(
    items: list[CandidateFinding],
    verdicts: dict[int, dict],
    *,
    judge_model: str,
    min_confidence: float,
) -> dict[str, int]:
    """把复核裁定写回结论，必要时把结论拉进人工复核队列。

    只加严不减严：复核说“不成立”或把握不足时转人工，但复核给的等级建议
    **不直接改写 risk_level**——等级口径以规则引擎为准，第二模型的异议
    只作为记录保留，避免复核模型自己的错把已定级的高风险改低。

    返回统计：{judged, disputed, unjudged}。
    """
    judged = disputed = unjudged = 0

    for index, item in enumerate(items):
        verdict = verdicts.get(index)
        if verdict is None:
            # 没拿到裁定 = 这一批复核失败，不能当作“复核通过”
            item.judge_verdict = {
                "judged": False,
                "judgedBy": judge_model,
                "reason": "本批未得到复核裁定（模型调用失败或返回不含该条）",
            }
            unjudged += 1
            continue

        agree = bool(verdict.get("agree", True))
        confidence = float(verdict.get("confidence", 0.0) or 0.0)
        item.judge_verdict = {
            "judged": True,
            "judgedBy": judge_model,
            "agree": agree,
            "citationOk": bool(verdict.get("citation_ok", verdict.get("citationOk", True))),
            "suggestedRiskLevel": verdict.get("suggested_risk_level") or verdict.get("suggestedRiskLevel"),
            "confidence": round(confidence, 4),
            "reason": str(verdict.get("reason") or "")[:500],
        }
        judged += 1

        if agree and confidence >= min_confidence:
            continue

        disputed += 1
        if item.human_review_status == HumanReviewStatus.PENDING.value:
            # 已因更硬的原因进队，不覆盖原 reason
            continue
        item.human_review_status = HumanReviewStatus.PENDING.value
        item.human_review_reason = HumanReviewReason.JUDGE_DISPUTED.value

    return {"judged": judged, "disputed": disputed, "unjudged": unjudged}
