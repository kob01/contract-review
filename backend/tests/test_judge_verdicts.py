"""结论复核（judge）的写回与切批逻辑。

复核是"第二道闸"，最容易出的事故是把它写成能放松路由的东西，
以及批次失败时把没复核过的结论当成复核通过。
"""

from __future__ import annotations

from app.models.enums import FindingSource, HumanReviewReason, HumanReviewStatus, RiskLevel
from app.services.findings import CandidateFinding, apply_judge_verdicts
from app.services.judge import _plan_batches


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
    batches = _plan_batches(items, max_items=2)
    flat = [index for batch in batches for index, _ in batch]
    assert flat == [0, 1, 2, 3, 4]
    assert [len(batch) for batch in batches] == [2, 2, 1]


def test_empty_input_produces_no_batches():
    assert _plan_batches([], max_items=3) == []
