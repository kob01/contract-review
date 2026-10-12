"""结论复核（judge）的写回与切批逻辑。

复核是"第二道闸"，最容易出的事故是把它写成能放松路由的东西，
以及批次失败时把没复核过的结论当成复核通过。
"""

from __future__ import annotations

from app.models.enums import FindingSource, HumanReviewReason, HumanReviewStatus, RiskLevel
from app.services.findings import CandidateFinding, apply_judge_verdicts
from app.services.judge import (
    RULE_CHECK_MODEL,
    _plan_batches,
    rule_check_verdict,
    run_rule_check_judge,
)


def make_item(**overrides) -> CandidateFinding:
    base = {
        "title": "综合融资成本超红线",
        "risk_level": RiskLevel.HIGH.value,
        "clause_category": "rate_and_fee",
        "analysis": "超过监管口径",
        "suggestion": "下调费率",
        "excerpt": "综合年化 36%",
        "source": FindingSource.RULE.value,
        "cross_check_status": "rule_only",
        "confidence": 1.0,
        "human_review_status": HumanReviewStatus.NOT_REQUIRED.value,
    }
    base.update(overrides)
    return CandidateFinding(**base)


def test_judge_disagreement_forces_human_review():
    items = [make_item()]
    counts = apply_judge_verdicts(
        items,
        {0: {"agree": False, "confidence": 0.9, "reason": "条款不含该费用"}},
        judge_model="glm-5.3-flash",
        min_confidence=0.6,
    )
    assert items[0].human_review_status == HumanReviewStatus.PENDING.value
    assert items[0].human_review_reason == HumanReviewReason.JUDGE_DISPUTED.value
    assert counts == {"judged": 1, "disputed": 1, "unjudged": 0}


def test_low_judge_confidence_forces_human_review():
    items = [make_item()]
    apply_judge_verdicts(
        items,
        {0: {"agree": True, "confidence": 0.4, "reason": "依据含糊"}},
        judge_model="glm-5.3-flash",
        min_confidence=0.6,
    )
    assert items[0].human_review_status == HumanReviewStatus.PENDING.value


def test_missing_verdict_is_marked_unjudged_not_passed():
    items = [make_item(), make_item(title="另一条")]
    counts = apply_judge_verdicts(
        items,
        {0: {"agree": True, "confidence": 0.8}},
        judge_model="glm-5.3-flash",
        min_confidence=0.6,
    )
    assert items[0].judge_verdict["judged"] is True
    assert items[1].judge_verdict["judged"] is False
    assert counts["unjudged"] == 1


def test_existing_pending_reason_is_not_overwritten():
    """已因引用未核验进队的结论，不该被复核意见换掉最主要原因。"""
    items = [
        make_item(
            human_review_status=HumanReviewStatus.PENDING.value,
            human_review_reason=HumanReviewReason.UNVERIFIED_CITATION.value,
        )
    ]
    apply_judge_verdicts(
        items,
        {0: {"agree": False, "confidence": 0.9}},
        judge_model="glm-5.3-flash",
        min_confidence=0.6,
    )
    assert items[0].human_review_reason == HumanReviewReason.UNVERIFIED_CITATION.value
    assert items[0].judge_verdict["agree"] is False


def test_judge_never_downgrades_risk_level():
    """复核建议把 high 改成 low 只记录，不改写等级。"""
    items = [make_item(risk_level=RiskLevel.HIGH.value)]
    apply_judge_verdicts(
        items,
        {0: {"agree": True, "confidence": 0.9, "suggested_risk_level": "low"}},
        judge_model="glm-5.3-flash",
        min_confidence=0.6,
    )
    assert items[0].risk_level == RiskLevel.HIGH.value
    assert items[0].judge_verdict["suggestedRiskLevel"] == "low"


def test_batches_use_global_index():
    items = [make_item(title=f"结论 {index}") for index in range(5)]
    batches = _plan_batches(items, max_items=2, char_budget=9000)
    flat = [index for batch in batches for index, _ in batch]
    assert flat == [0, 1, 2, 3, 4]
    assert [len(batch) for batch in batches] == [2, 2, 1]


def test_batches_split_on_char_budget_not_only_item_count():
    """字符预算必须能单独切批：否则一批 12 条长分析会撞爆复核模型的上下文。"""
    items = [make_item(title=f"结论 {index}", analysis="很长" * 400) for index in range(3)]
    batches = _plan_batches(items, max_items=12, char_budget=1500)
    assert [len(batch) for batch in batches] == [1, 1, 1]


def test_empty_input_produces_no_batches():
    assert _plan_batches([], max_items=3, char_budget=9000) == []


# ---------- 确定性规则复核（回音壁降级）----------


def test_rule_check_rejects_findings_without_verified_citation():
    """引用全部未核验时不能判“成立”：同模型复核只会附和，硬信号不会。"""
    item = make_item(citations=[{"lawName": "办法", "articleNo": "第十八条", "verified": False}])
    verdict = rule_check_verdict(0, item)
    assert verdict["agree"] is False
    assert verdict["citation_ok"] is False


def test_rule_check_rejects_uncited_and_unlocated_findings():
    no_citation = rule_check_verdict(0, make_item(citations=[]))
    assert no_citation["agree"] is False

    unlocated = make_item(
        source=FindingSource.LLM.value,
        citations=[{"lawName": "民法典", "articleNo": "第六百七十条", "verified": True}],
        located=False,
    )
    assert rule_check_verdict(1, unlocated)["agree"] is False


def test_rule_check_accepts_rule_hit_with_verified_citation():
    item = make_item(
        citations=[{"lawName": "办法", "articleNo": "第十八条", "verified": True}],
        rule_confidence=1.0,
    )
    verdict = rule_check_verdict(0, item)
    assert verdict["agree"] is True
    assert verdict["confidence"] >= 0.9


def test_rule_check_marks_conflicting_channels_as_disputed():
    """双通道定性冲突属“以规则为准但需人工确认”，不能当复核通过。"""
    item = make_item(
        cross_check_status="rule_overrides",
        citations=[{"lawName": "办法", "articleNo": "第十八条", "verified": True}],
    )
    assert rule_check_verdict(0, item)["agree"] is False


def test_rule_check_outcome_routes_disputed_into_human_review():
    """降级复核写回后仍然“只加严”：不成立进人工队列，等级不变。"""
    items = [make_item(citations=[]), make_item(title="已核验的一条", citations=[
        {"lawName": "办法", "articleNo": "第十八条", "verified": True}
    ])]
    outcome = run_rule_check_judge(items)
    assert outcome.model == RULE_CHECK_MODEL
    assert outcome.executed

    counts = apply_judge_verdicts(
        items, outcome.verdicts, judge_model=outcome.model, min_confidence=0.6
    )
    assert counts["judged"] == 2
    assert items[0].human_review_status == HumanReviewStatus.PENDING.value
    assert items[0].risk_level == RiskLevel.HIGH.value
    assert items[1].human_review_status == HumanReviewStatus.NOT_REQUIRED.value
