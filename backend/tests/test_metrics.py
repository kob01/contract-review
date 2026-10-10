"""指标抽取：利率口径、IRR、砍头息与霸王条款特征。

这些数字直接决定红线判定结果，测试用手工算好的期望值，
而不是"跑一遍把输出填回去"——后者等于让实现自证正确。
"""

from __future__ import annotations

import pytest
from app.services.rules.metrics import (
    MetricSet,
    _parse_chinese_number,
    _solve_irr,
    extract_metrics,
    metric_summary_for_prompt,
)

from tests.fixtures import build_document


@pytest.fixture(scope="module")
def metrics() -> MetricSet:
    document = build_document()
    from app.services.parser.segmenter import segmentize

    return extract_metrics(document, segmentize(document))


def test_chinese_number_parsing():
    assert _parse_chinese_number("五") == 5
    assert _parse_chinese_number("十二") == 12
    assert _parse_chinese_number("三十五") == 35
    assert _parse_chinese_number("一百") == 100


def test_daily_rate_from_wanfen_zhi_wu(metrics: MetricSet):
    """日息万分之五 → 日利率 0.05%，单利年化 18.25%。"""
    daily = metrics.value_of("daily_rate")
    assert daily is not None
    assert daily.value == pytest.approx(0.0005, abs=1e-9)

    effective = metrics.value_of("effective_annual_rate")
    assert effective is not None
    assert effective.value == pytest.approx(0.1825, abs=1e-4)


def test_monthly_fee_is_annualized(metrics: MetricSet):
    """每月按本金 1% 收担保费 → 复利年化约 12.68%。"""
    fee = metrics.value_of("monthly_fee_rate")
    assert fee is not None
    assert fee.value == pytest.approx(0.01, abs=1e-9)

    annualized = [
        item.value for item in metrics.by_key("comprehensive_annual_rate") if item.value
    ]
    assert any(value == pytest.approx(0.1268, abs=1e-3) for value in annualized)
    # value_of 取最不利口径，应高于仅由费用折算出的那个值
    assert metrics.value_of("comprehensive_annual_rate").value == max(annualized)


def test_stated_comprehensive_rate_is_captured(metrics: MetricSet):
    stated = metrics.value_of("comprehensive_annual_rate")
    assert stated is not None
    # 36% 的直述值应当大于费用折算值，取最不利口径
    assert max(item.value or 0 for item in metrics.by_key("comprehensive_annual_rate")) == pytest.approx(
        0.36, abs=1e-6
    )


def test_max_annual_cost_is_the_worst_measure(metrics: MetricSet):
    """多口径并存时必须取最高年化，否则"以费代息"会被平均掉。"""
    annual_keys = (
        "nominal_annual_rate",
        "comprehensive_annual_rate",
        "effective_annual_rate",
        "irr_annual_rate",
    )
    annual_values = [
        item.value for key in annual_keys for item in metrics.by_key(key) if item.value
    ]
    assert annual_values, "样本合同应当至少抽出一个年化口径"

    worst = metrics.derived["max_annual_cost_rate"]
    assert worst == max(annual_values)
    # 本样本里现金流 IRR 高于表面写的 36%，取 IRR 才不会被表面的“不超过 36%”骗到
    assert worst > 0.36


def test_derived_does_not_shadow_evidenced_metrics(metrics: MetricSet):
    """derived 只装无单一出处的聚合值，否则规则命中的原文溯源会丢。"""
    for key, reading in ((k, metrics.value_of(k)) for k in metrics.derived):
        assert key not in {item.key for item in metrics.evidences if item.value is not None}, key
        assert reading is not None


def test_solve_irr_zeroes_net_present_value():
    """本金 10 万、12 期、每期 1 万 → 总付 12 万，月度 IRR 约 3%。"""
    cashflows = [100_000.0] + [-10_000.0] * 12
    monthly = _solve_irr(cashflows)
    assert monthly is not None

    npv = sum(cf / (1 + monthly) ** t for t, cf in enumerate(cashflows))
    assert abs(npv) < 1.0, "求得的 IRR 应当使现金流现值近于零"
    assert 0.02 < monthly < 0.04
    assert (1 + monthly) ** 12 - 1 == pytest.approx(0.42, abs=0.06)


def test_irr_metric_present_and_exceeds_daily_annualization(metrics: MetricSet):
    irr = metrics.value_of("irr_annual_rate")
    assert irr is not None
    # 每期固定还款的现金流口径下，年化远高于仅按日息折算出的口径
    assert irr.value > 0.25


def test_head_interest_shortfall_is_quantified(metrics: MetricSet):
    """借款 10 万、实际到账 9.5 万 → 预扣 5%。"""
    shortfall = metrics.value_of("disbursement_shortfall_ratio")
    assert shortfall is not None
    assert shortfall.value == pytest.approx(0.05, abs=1e-6)
    assert shortfall.evidence is not None, "预扣比例必须带原文出处，否则无法溯源"
    assert "95,000" in shortfall.evidence.excerpt


def test_unfair_clause_flags_are_detected(metrics: MetricSet):
    expected = {
        "waive_procedural_rights",
        "final_interpretation",
        "unilateral_price_change",
        "mandatory_insurance",
        "blank_deduction_authorization",
        "lender_home_jurisdiction",
        "acceleration_broad",
        "head_interest_deduction",
    }
    detected = {item.key for item in metrics.evidences if item.kind == "flag"}
    assert expected <= detected, f"未识别出：{expected - detected}"


def test_every_flag_carries_traceable_location(metrics: MetricSet):
    """没有原文出处的特征一律不合格——溯源高亮依赖这些偏移。"""
    for item in metrics.evidences:
        assert item.excerpt, f"{item.key} 缺少原文引用"
        assert item.char_start is not None and item.char_end > item.char_start
        assert item.page_no == 1


def test_missing_rate_yields_no_worst_value_and_warns():
    document = build_document("本协议约定双方权利义务，未提及任何利率或费用。")
    from app.services.parser.segmenter import segmentize

    result = extract_metrics(document, segmentize(document))
    assert "max_annual_cost_rate" not in result.derived
    assert result.has_flag("annual_cost_not_disclosed")
    assert result.warnings


def test_metric_summary_for_prompt_contains_derivation(metrics: MetricSet):
    text = metric_summary_for_prompt(metrics)
    assert "日利率" in text
    assert "%" in text


def test_metricset_roundtrip(metrics: MetricSet):
    """解析结果落库后再读出，必须等价（审查阶段依赖这个往返）。"""
    restored = MetricSet.from_dict(metrics.to_dict())
    assert restored.derived == metrics.derived
    assert len(restored.evidences) == len(metrics.evidences)
    assert restored.value_of("daily_rate").value == pytest.approx(0.0005, abs=1e-9)
