"""交叉校验与置信度路由。

这两块决定了"哪些结论可以直接出报告"，出错方向是漏放高风险条款，
因此对每一条路由规则都单独设测。
"""

from __future__ import annotations

from app.models.enums import (
    CrossCheckStatus,
    FindingSource,
    HumanReviewReason,
    HumanReviewStatus,
    RiskLevel,
)
from app.services.findings import (
    CandidateFinding,
    apply_routing,
    merge_and_cross_check,
    route_reason,
)


def make_item(**overrides) -> CandidateFinding:
    base = {
        "title": "条款问题",
        "risk_level": RiskLevel.HIGH.value,
        "clause_category": "rate_and_fee",
        "analysis": "分析",
        "suggestion": "建议",
        "excerpt": "综合年化融资成本 36%",
        "source": FindingSource.RULE.value,
        "cross_check_status": CrossCheckStatus.RULE_ONLY.value,
        "confidence": 1.0,
        "rule_code": "RATE-001",
        "segment_index": 2,
        "char_start": 100,
        "char_end": 130,
        "citations": [{"provisionId": "p-1", "verified": True}],
    }
    base.update(overrides)
    return CandidateFinding(**base)


# ---------- 交叉校验 ----------


def test_same_segment_same_level_becomes_consistent():
    rule_item = make_item()
    llm_item = make_item(
        source=FindingSource.LLM.value,
        confidence=0.9,
        rule_code=None,
        citations=[{"provisionId": "p-9", "verified": True}],
    )

    merged, conflicts = merge_and_cross_check([rule_item], [llm_item])
    assert conflicts == 0
    assert len(merged) == 1
    assert merged[0].source == FindingSource.BOTH.value
    assert merged[0].cross_check_status == CrossCheckStatus.CONSISTENT.value
    # 双通道一致时置信度封顶为 1
    assert merged[0].confidence <= 1.0
    assert merged[0].rule_confidence == 1.0
    assert merged[0].llm_confidence == 0.9


def test_risk_level_conflict_is_resolved_in_favour_of_rule_engine():
    rule_item = make_item(risk_level=RiskLevel.HIGH.value)
    llm_item = make_item(
        source=FindingSource.LLM.value,
        risk_level=RiskLevel.LOW.value,
        confidence=0.95,
        analysis="模型认为不构成风险",
    )

    merged, conflicts = merge_and_cross_check([rule_item], [llm_item])
    assert conflicts == 1
    item = merged[0]
    assert item.cross_check_status == CrossCheckStatus.RULE_OVERRIDES.value
    assert item.risk_level == RiskLevel.HIGH.value, "冲突时必须以规则引擎等级为准"
    assert "[模型不同意见]" in item.analysis


def test_rule_code_match_links_items_across_different_segments():
    rule_item = make_item(segment_index=2, char_start=100, char_end=130)
    llm_item = make_item(
        source=FindingSource.LLM.value,
        rule_code="RATE-001",
        segment_index=7,
        char_start=900,
        char_end=940,
    )

    merged, _ = merge_and_cross_check([rule_item], [llm_item])
    assert len(merged) == 1
    assert merged[0].source == FindingSource.BOTH.value


def test_non_overlapping_items_stay_separate():
    rule_item = make_item()
    llm_item = make_item(
        source=FindingSource.LLM.value,
        rule_code=None,
        segment_index=9,
        clause_category="dispute",
        char_start=800,
        char_end=860,
    )

    merged, conflicts = merge_and_cross_check([rule_item], [llm_item])
    assert len(merged) == 2
    assert conflicts == 0
    sources = {item.source for item in merged}
    assert sources == {FindingSource.RULE.value, FindingSource.LLM.value}


def test_char_range_overlap_is_detected():
    rule_item = make_item(char_start=100, char_end=200, segment_index=None)
    llm_item = make_item(
        source=FindingSource.LLM.value,
        rule_code=None,
        segment_index=None,
        clause_category="rate_and_fee",
        char_start=150,
        char_end=210,
    )

    merged, _ = merge_and_cross_check([rule_item], [llm_item])
    assert len(merged) == 1, "字符区间大幅重叠应识别为同一处问题"


# ---------- 置信度路由 ----------


def test_low_confidence_llm_finding_goes_to_human():
    item = make_item(
        source=FindingSource.LLM.value,
        cross_check_status=CrossCheckStatus.LLM_ONLY.value,
        confidence=0.7,
        risk_level=RiskLevel.MEDIUM.value,
    )
    assert route_reason(item, threshold=0.8, mandatory_high_risk=True) is (
        HumanReviewReason.LOW_CONFIDENCE
    )


def test_rule_hard_hit_above_threshold_does_not_need_human_by_confidence():
    """规则硬命中的结论不靠模型置信度，除非引用未核验或高风险强制。"""
    item = make_item(risk_level=RiskLevel.MEDIUM.value, confidence=1.0)
    assert route_reason(item, threshold=0.8, mandatory_high_risk=False) is None


def test_unverified_citation_forces_review_even_for_rule_hits():
    item = make_item(
        risk_level=RiskLevel.MEDIUM.value,
        citations=[{"provisionId": None, "verified": False, "verifyReason": "知识库未收录"}],
    )
    assert route_reason(item, threshold=0.8, mandatory_high_risk=False) is (
        HumanReviewReason.UNVERIFIED_CITATION
    )


def test_high_risk_mandatory_review_can_be_enabled():
    item = make_item(risk_level=RiskLevel.HIGH.value)
    assert route_reason(item, threshold=0.8, mandatory_high_risk=True) is (
        HumanReviewReason.HIGH_RISK_MANDATORY
    )


def test_conflict_reason_wins_over_other_reasons():
    item = make_item(
        cross_check_status=CrossCheckStatus.RULE_OVERRIDES.value,
        confidence=0.5,
        citations=[{"verified": False}],
        risk_level=RiskLevel.LOW.value,
    )
    assert route_reason(item, threshold=0.8, mandatory_high_risk=True) is (
        HumanReviewReason.RULE_LLM_CONFLICT
    )


def test_unlocated_excerpt_forces_review():
    item = make_item(risk_level=RiskLevel.MEDIUM.value, located=False, confidence=1.0)
    assert route_reason(item, threshold=0.8, mandatory_high_risk=False) is (
        HumanReviewReason.METRIC_MISSING
    )


def test_apply_routing_writes_status_and_buckets():
    items = [
        make_item(),  # 高风险 + 强制确认 → 进队列
        make_item(
            rule_code="PROT-001",
            risk_level=RiskLevel.MEDIUM.value,
            confidence=1.0,
        ),  # 规则 + 已核验 + 非高风险 → 不进队列
    ]
    buckets = apply_routing(items, threshold=0.8, mandatory_high_risk=True)

    assert items[0].human_review_status == HumanReviewStatus.PENDING.value
    assert items[0].human_review_reason == HumanReviewReason.HIGH_RISK_MANDATORY.value
    assert items[1].human_review_status == HumanReviewStatus.NOT_REQUIRED.value
    assert set(buckets) == {"RATE-001"}
    assert buckets["RATE-001"][0][1] is HumanReviewReason.HIGH_RISK_MANDATORY
