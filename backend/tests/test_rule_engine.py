"""规则引擎判定。

覆盖三类最容易出事的情形：
硬指标该拦的没拦（漏报）、指标抽不出来却当成通过（假阴性）、
以及内置规则的适用范围写错（错误定性）。
"""

from __future__ import annotations

import pytest
from app.core.config import Settings
from app.models.enums import ContractKind, RiskLevel
from app.models.redline import RedLineRule
from app.services.parser.segmenter import segmentize
from app.services.rules.builtin_redlines import as_rule_rows
from app.services.rules.engine import RuleEngine
from app.services.rules.metrics import extract_metrics

from tests.fixtures import build_document


@pytest.fixture(scope="module")
def rules() -> list[RedLineRule]:
    return [RedLineRule(**row) for row in as_rule_rows()]


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(
        lpr_one_year_rate=0.0345,
        lpr_multiple=4.0,
        regulatory_cost_ceiling=0.24,
        historical_hard_ceiling=0.36,
    )


def evaluate(text: str, rules, settings, kind: str = ContractKind.CONSUMER_LOAN.value):
    document = build_document(text)
    segments = segmentize(document)
    metrics = extract_metrics(document, segments)
    engine = RuleEngine(rules, settings)
    return metrics, engine.evaluate(
        metrics, full_text=document.text, segments=segments, contract_kind=kind
    )


def test_rate_over_24_percent_is_hard_blocked(rules, settings):
    _metrics, evaluation = evaluate(build_document().text, rules, settings)

    hit = next((item for item in evaluation.hits if item.rule_code == "RATE-001"), None)
    assert hit is not None, "36%/IRR 41% 的样本必须命中 24% 红线"
    assert hit.hard_block is True
    assert hit.confidence == 1.0
    assert hit.severity == RiskLevel.HIGH.value
    assert "24.00%" in hit.threshold_desc
    assert hit.metric_value == pytest.approx(0.413, abs=0.01)


def test_head_interest_rule_hits_on_disbursement_gap(rules, settings):
    _metrics, evaluation = evaluate(build_document().text, rules, settings)
    hit = next((item for item in evaluation.hits if item.rule_code == "FEE-001"), None)
    assert hit is not None
    assert hit.metric_value == pytest.approx(0.05, abs=1e-6)
    assert "95,000" in (hit.evidence.excerpt if hit.evidence else "")


def test_unfair_clause_rules_hit_via_flags(rules, settings):
    _metrics, evaluation = evaluate(build_document().text, rules, settings)
    codes = {hit.rule_code for hit in evaluation.hits}
    assert {"PROT-001", "PROT-002", "PROT-005"} <= codes


def test_missing_rate_makes_rate_rule_inconclusive_not_pass(rules, settings):
    text = "第一条 合作范围\n双方就导流推广事宜开展合作，具体费用另行协商。"
    _metrics, evaluation = evaluate(text, rules, settings)

    codes_hit = {hit.rule_code for hit in evaluation.hits}
    assert "RATE-001" not in codes_hit, "抽不到利率时不得凭空判定超限"
    assert "RATE-001" in {code for code, _ in evaluation.inconclusive}
    # RATE-004 是"存在性"规则，没有年化口径正好命中
    assert "RATE-004" in codes_hit


def test_private_lending_rule_only_applies_to_that_kind(rules, settings):
    text = "第一条 借款利率\n年利率为 40%，借款期限 12 个月。"

    _metrics, consumer = evaluate(text, rules, settings, ContractKind.CONSUMER_LOAN.value)
    _metrics, private = evaluate(text, rules, settings, ContractKind.PRIVATE_LENDING.value)

    assert "RATE-002" not in {hit.rule_code for hit in consumer.hits}
    hit = next((item for item in private.hits if item.rule_code == "RATE-002"), None)
    assert hit is not None
    # 4 × 3.45% = 13.8%
    assert hit.threshold == pytest.approx(0.138, abs=1e-4)
    assert "13.80%" in hit.threshold_desc


def test_secondary_threshold_escalates_severity(rules, settings):
    text = "第一条 借款利率\n年利率为 40%，借款期限 12 个月。"
    _metrics, evaluation = evaluate(text, rules, settings, ContractKind.PRIVATE_LENDING.value)
    hit = next(item for item in evaluation.hits if item.rule_code == "RATE-002")
    assert hit.severity == RiskLevel.HIGH.value
    assert "更严格的历史红线" in hit.analysis


def test_irr_divergence_rule_needs_nominal_rate(rules, settings):
    """IRR 与名义利率背离规则：缺名义利率时应判为无法定量，而非放过。"""
    without_nominal = "第一条 还款\n借款金额 100000 元，借款期限 12 个月，每期还款金额 15000 元。"
    _metrics, evaluation = evaluate(without_nominal, rules, settings)
    assert "RATE-005" in {code for code, _ in evaluation.inconclusive}

    with_nominal = (
        "第一条 利率与还款\n借款金额 100000 元，年利率为 10%，"
        "借款期限 12 个月，每期还款金额 15000 元。"
    )
    _metrics2, evaluation2 = evaluate(with_nominal, rules, settings)
    hit = next((item for item in evaluation2.hits if item.rule_code == "RATE-005"), None)
    assert hit is not None
    assert hit.threshold == pytest.approx(0.15, abs=1e-6)
    assert hit.metric_value > hit.threshold


def test_checked_rule_count_respects_applies_to(rules, settings):
    text = "第一条 其他\n本协议不涉及贷款业务。"
    _metrics, consumer = evaluate(text, rules, settings, ContractKind.CONSUMER_LOAN.value)
    _metrics, guarantee = evaluate(
        text, rules, settings, ContractKind.GUARANTEE.value
    )
    assert consumer.checked_rule_count != guarantee.checked_rule_count


def test_rate_002_cites_the_lpr_cap_article_not_the_head_interest_one(rules):
    """锁定引用条号：现行法释〔2020〕17号里 4 倍 LPR 是第二十五条。

    第二十六条讲的是"债权凭证载明金额认定为本金"（砍头息）。曾经引错成第二十六条，
    后果是导入正确原文后 `verified=True`、前端照常展示，却拿砍头息条文给利率超限背书。
    """
    row = next(item for item in as_rule_rows() if item["code"] == "RATE-002")
    refs = [ref for ref in row["basis_refs"] if "民间借贷" in ref["law_name"]]
    assert len(refs) == 1
    assert refs[0]["article_no"] == "第二十五条"


def test_brch_002_does_not_fire_on_negated_joint_collection(rules, settings):
    """"不重复计收"是合规表述，正则不得读成违规。"""
    compliant = (
        "第三条 逾期\n乙方逾期的，甲方按未还本金每日万分之三计收罚息，"
        "罚息与违约金不重复计收。"
    )
    violating = "第三条 逾期\n乙方逾期的，逾期利息、违约金同时计收。"

    _metrics, ok = evaluate(compliant, rules, settings)
    assert "BRCH-002" not in {hit.rule_code for hit in ok.hits}

    _metrics, bad = evaluate(violating, rules, settings)
    assert "BRCH-002" in {hit.rule_code for hit in bad.hits}, "收紧正则后真违规仍须命中"


def test_private_lending_ceiling_is_four_times_the_current_lpr():
    """12.2% 在 4×3.0%=12.0% 下应命中；LPR 停在 3.1%（上限 12.4%）时这段区间整片漏报。"""
    current = Settings(
        lpr_one_year_rate=0.030,
        lpr_multiple=4.0,
        regulatory_cost_ceiling=0.24,
        historical_hard_ceiling=0.36,
    )
    stale = Settings(
        lpr_one_year_rate=0.031,
        lpr_multiple=4.0,
        regulatory_cost_ceiling=0.24,
        historical_hard_ceiling=0.36,
    )
    text = (
        "第一条 借款金额\n借款金额为 500,000 元。\n"
        "第二条 利息\n双方约定年利率为 12.2%。\n"
        "第三条 还款\n借款期限 12 个月，到期一次性归还本金及利息。"
    )
    rules = [RedLineRule(**row) for row in as_rule_rows()]

    _metrics, hit_now = evaluate(
        text, rules, current, kind=ContractKind.PRIVATE_LENDING.value
    )
    _metrics, hit_stale = evaluate(
        text, rules, stale, kind=ContractKind.PRIVATE_LENDING.value
    )

    assert "RATE-002" in {hit.rule_code for hit in hit_now.hits}
    assert "RATE-002" not in {hit.rule_code for hit in hit_stale.hits}


def test_hit_carries_basis_refs_for_citation_check(rules, settings):
    _metrics, evaluation = evaluate(build_document().text, rules, settings)
    hit = next(item for item in evaluation.hits if item.rule_code == "RATE-001")
    assert hit.basis_refs, "红线必须带引用依据线索，否则无法做溯源核验"
