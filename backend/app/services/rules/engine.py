"""规则引擎：把合规红线规则解释执行在抽取指标上。

与 LLM 通道的分工是这套架构的立场所在：
- 能用数确定性判定的（利率、费用率、期限、比例），一律在此定性，置信度 1.0，
  且 `hard_block` 规则命中时 LLM 的相反结论不得覆盖；
- 规则只能给出"弱信号"的（正则命中的可疑表述）标记 `needs_llm_review`，
  交 LLM + RAG 定性；
- 规则所需的指标抽不出来时记为 `inconclusive` 而不是"通过"，
  并把该规则作为重点检查项注入模型提示词。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.enums import MetricOperator, RiskLevel
from app.models.redline import RedLineRule
from app.services.rules.metrics import Evidence, MetricSet

logger = get_logger(__name__)

WEAK_SIGNAL_CONFIDENCE = 0.75


@dataclass(slots=True)
class RuleHit:
    """一次规则命中，字段与 `ReviewFinding` 对齐，便于 pipeline 直接落库。"""

    rule_code: str
    rule_name: str
    category: str
    severity: str
    hard_block: bool
    title: str
    analysis: str
    suggestion: str
    confidence: float
    needs_llm_review: bool
    metric_key: str | None = None
    metric_value: float | None = None
    threshold: float | None = None
    threshold_desc: str = ""
    derivation: str = ""
    evidence: Evidence | None = None
    basis_refs: list[dict] = field(default_factory=list)

    @property
    def metric_snapshot(self) -> dict:
        return {
            "metricKey": self.metric_key,
            "metricValue": self.metric_value,
            "threshold": self.threshold,
            "thresholdDesc": self.threshold_desc,
            "derivation": self.derivation,
            "ruleCode": self.rule_code,
        }


@dataclass(slots=True)
class RuleEvaluation:
    hits: list[RuleHit] = field(default_factory=list)
    #: 因指标缺失而无法判定的规则编码
    inconclusive: list[tuple[str, str]] = field(default_factory=list)
    #: 需要 LLM 复核的弱信号规则编码
    weak_signals: list[str] = field(default_factory=list)
    checked_rule_count: int = 0

    @property
    def hard_hits(self) -> list[RuleHit]:
        return [hit for hit in self.hits if hit.hard_block]


class RuleEngine:
    def __init__(self, rules: Sequence[RedLineRule], app_settings: Settings) -> None:
        self._rules = list(rules)
        self._settings = app_settings

    # ---------- 阈值解析 ----------

    def resolve_threshold(self, rule: RedLineRule, metrics: MetricSet) -> tuple[float | None, str]:
        """返回 (阈值比率, 人类可读说明)。比率统一为小数口径。"""
        source = rule.threshold_source or "static"

        if source == "lpr_four_multiple":
            value = self._settings.lpr_one_year_rate * self._settings.lpr_multiple
            desc = (
                f"一年期 LPR {self._settings.lpr_one_year_rate:.2%} 的 "
                f"{self._settings.lpr_multiple:g} 倍 = {value:.2%}"
            )
            return value, desc
        if source == "regulatory_cost_ceiling":
            value = self._settings.regulatory_cost_ceiling
            return value, f"监管综合融资成本红线 {value:.2%}"
        if source == "historical_hard_ceiling":
            value = self._settings.historical_hard_ceiling
            return value, f"历史高利贷参考线 {value:.2%}"
        if source == "nominal_rate_multiple":
            # 用于“真实 IRR 显著高于名义利率”这类背离型判定
            base = metrics.value_of("nominal_annual_rate")
            if base is None or rule.threshold_value is None:
                return None, "缺少名义年利率，无法折算比较基准"
            value = base.value * rule.threshold_value
            return value, (
                f"名义年利率 {base.value:.2%} 的 {rule.threshold_value:g} 倍 = {value:.2%}"
            )

        if rule.threshold_value is None:
            return None, "未配置阈值"
        value = rule.threshold_value
        if rule.unit == "percent":
            value = value / 100.0
        return value, f"阈值 {value:.2%}" if rule.unit == "percent" else f"阈值 {value:g}"

    # ---------- 主流程 ----------

    def evaluate(
        self,
        metrics: MetricSet,
        *,
        full_text: str,
        segments: list[dict],
        contract_kind: str,
    ) -> RuleEvaluation:
        result = RuleEvaluation()

        for rule in self._rules:
            if not rule.is_active:
                continue
            if rule.applies_to and contract_kind not in rule.applies_to:
                continue

            result.checked_rule_count += 1
            hit = self._evaluate_one(rule, metrics, full_text, segments)

            if hit is None:
                continue
            if hit == "inconclusive":
                if rule.llm_fallback:
                    result.inconclusive.append((rule.code, rule.name))
                continue

            result.hits.append(hit)
            if hit.needs_llm_review:
                result.weak_signals.append(hit.rule_code)

        return result

    def _evaluate_one(
        self, rule: RedLineRule, metrics: MetricSet, full_text: str, segments: list[dict]
    ) -> RuleHit | str | None:
        """返回命中、"inconclusive" 标记或 None（明确不命中）。"""
        if rule.has_metric_condition:
            outcome = self._evaluate_metric(rule, metrics)
            if outcome != "inconclusive":
                return outcome
            # 指标缺失时退回文本特征
            if not rule.keyword_patterns:
                return "inconclusive"

        return self._evaluate_keywords(rule, full_text, segments)

    def _evaluate_metric(self, rule: RedLineRule, metrics: MetricSet) -> RuleHit | str | None:
        reading = metrics.value_of(rule.metric_key or "")
        operator = rule.operator_enum

        if operator in (MetricOperator.EXISTS, MetricOperator.MISSING):
            present = reading is not None
            hit = present if operator is MetricOperator.EXISTS else not present
            if not hit:
                return None
            return self._build_hit(
                rule,
                reading=reading,
                threshold=None,
                threshold_desc="条款存在性判定",
                extra="合同中出现该表述。" if present else "合同中缺少该表述。",
            )

        if reading is None:
            return "inconclusive"

        threshold, threshold_desc = self.resolve_threshold(rule, metrics)
        if threshold is None:
            # 比较基准本身抽不出来（如需名义利率而合同未写），同样属于无法判定
            logger.info("规则 %s 无法解析阈值：%s", rule.code, threshold_desc)
            return "inconclusive"

        if not self._compare(reading.value, operator, threshold, rule.tolerance):
            return None

        extra = (
            f"实测值 {self._format_value(rule, reading.value)}，"
            f"{threshold_desc}，判定为超出上限。"
            if operator in (MetricOperator.GT, MetricOperator.GTE)
            else f"实测值 {self._format_value(rule, reading.value)}，{threshold_desc}。"
        )
        hit = self._build_hit(
            rule,
            reading=reading,
            threshold=threshold,
            threshold_desc=threshold_desc,
            extra=extra,
        )

        # 二次阈值用于分级：超过 24% 提示，超过 36% 直接定性高风险
        if rule.secondary_threshold is not None and reading.value >= rule.secondary_threshold:
            hit.severity = RiskLevel.HIGH.value
            hit.analysis += " 且已超过更严格的历史红线，属重大合规问题。"
        return hit

    def _compare(self, value: float, operator: MetricOperator | None, threshold: float, tolerance: float) -> bool:
        if operator is None:
            return False
        epsilon = tolerance or 0.0
        if operator is MetricOperator.GT:
            return value > threshold + epsilon
        if operator is MetricOperator.GTE:
            return value >= threshold - epsilon
        if operator is MetricOperator.LT:
            return value < threshold - epsilon
        if operator is MetricOperator.LTE:
            return value <= threshold + epsilon
        if operator is MetricOperator.EQ:
            return abs(value - threshold) <= (epsilon or 1e-9)
        return False

    def _evaluate_keywords(
        self, rule: RedLineRule, full_text: str, segments: list[dict]
    ) -> RuleHit | None:
        """文本特征判定：正则命中只给出弱信号，默认需要 LLM 与人工确认。"""
        for pattern in rule.keyword_patterns or []:
            try:
                match = re.search(pattern, full_text)
            except re.error as exc:
                logger.warning("规则 %s 的正则无法编译：%s", rule.code, exc)
                continue
            if not match:
                continue

            evidence = self._build_keyword_evidence(rule, full_text, segments, match)
            return self._build_hit(
                rule,
                reading=None,
                threshold=None,
                threshold_desc="文本特征命中",
                extra=f"命中表述：“{evidence.excerpt}”",
                evidence=evidence,
                confidence=WEAK_SIGNAL_CONFIDENCE,
                needs_llm_review=not rule.hard_block,
            )
        return None

    def _build_keyword_evidence(
        self, rule: RedLineRule, full_text: str, segments: list[dict], match: re.Match[str]
    ) -> Evidence:
        from app.services.parser.segmenter import locate_excerpt

        start, end = match.start(), match.end()
        position = locate_excerpt(segments, full_text, full_text[start:end]) or {
            "segment_index": None,
            "char_start": start,
            "char_end": end,
            "page_no": 1,
        }
        return Evidence(
            key=rule.code,
            label=rule.name,
            kind="flag",
            value=None,
            display="",
            derivation=rule.guidance_text,
            excerpt=re.sub(r"\s+", " ", full_text[start:end]).strip(),
            char_start=position["char_start"],
            char_end=position["char_end"],
            segment_index=position["segment_index"],
            page_no=position["page_no"],
        )

    def _build_hit(
        self,
        rule: RedLineRule,
        *,
        reading,
        threshold: float | None,
        threshold_desc: str,
        extra: str,
        evidence: Evidence | None = None,
        confidence: float | None = None,
        needs_llm_review: bool = False,
    ) -> RuleHit:
        used_evidence = evidence or (reading.evidence if reading is not None else None)
        derivation = used_evidence.derivation if used_evidence else ""
        analysis = " ".join(part for part in (extra, rule.guidance_text) if part).strip()

        return RuleHit(
            rule_code=rule.code,
            rule_name=rule.name,
            category=rule.category,
            severity=rule.severity,
            hard_block=rule.hard_block,
            title=f"规则命中：{rule.name}",
            analysis=analysis,
            suggestion=rule.suggestion_text,
            confidence=confidence if confidence is not None else rule.hit_confidence,
            needs_llm_review=needs_llm_review,
            metric_key=rule.metric_key,
            metric_value=reading.value if reading is not None else None,
            threshold=threshold,
            threshold_desc=threshold_desc,
            derivation=derivation,
            evidence=used_evidence,
            basis_refs=list(rule.basis_refs or []),
        )

    def _format_value(self, rule: RedLineRule, value: float) -> str:
        if rule.unit in ("percent", "ratio") or (rule.metric_key or "").endswith("rate"):
            return f"{value:.2%}"
        return f"{value:g}"
