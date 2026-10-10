"""红线规则 Schema。"""

from __future__ import annotations

import uuid

from app.models.enums import ClauseCategory, ContractKind, MetricOperator, RiskLevel
from app.schemas.common import CamelModel


class RedLineRuleOut(CamelModel):
    id: uuid.UUID
    code: str
    name: str
    category: ClauseCategory
    severity: RiskLevel
    applies_to: list[str] = []
    metric_key: str | None = None
    operator: MetricOperator | None = None
    threshold_source: str = "static"
    threshold_value: float | None = None
    unit: str = "ratio"
    tolerance: float = 0.0
    secondary_threshold: float | None = None
    keyword_patterns: list[str] = []
    hard_block: bool = False
    llm_fallback: bool = True
    hit_confidence: float = 1.0
    basis_refs: list[dict] = []
    guidance_text: str = ""
    suggestion_text: str = ""
    priority: int = 100
    is_builtin: bool = False
    is_active: bool = True


class RedLineRuleUpsertRequest(CamelModel):
    code: str
    name: str
    category: ClauseCategory = ClauseCategory.OTHER
    severity: RiskLevel = RiskLevel.MEDIUM
    applies_to: list[ContractKind] = []
    metric_key: str | None = None
    operator: MetricOperator | None = None
    threshold_source: str = "static"
    threshold_value: float | None = None
    unit: str = "ratio"
    tolerance: float = 0.0
    secondary_threshold: float | None = None
    keyword_patterns: list[str] = []
    hard_block: bool = False
    llm_fallback: bool = True
    hit_confidence: float = 1.0
    basis_refs: list[dict] = []
    guidance_text: str = ""
    suggestion_text: str = ""
    priority: int = 100
    is_active: bool = True


class RuleDryRunRequest(CamelModel):
    """规则试算：粘贴一段合同文本，看会命中哪些红线。

    调阈值时不用发起完整审查即可回归，是规则库可维护性的关键。
    """

    text: str
    contract_kind: ContractKind = ContractKind.CONSUMER_LOAN
    rule_codes: list[str] = []


class RuleHitOut(CamelModel):
    rule_code: str
    rule_name: str
    severity: RiskLevel
    category: ClauseCategory
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
    excerpt: str = ""
    page_no: int | None = None
    segment_index: int | None = None
    basis_refs: list[dict] = []


class RuleDryRunResponse(CamelModel):
    hits: list[RuleHitOut]
    inconclusive: list[dict] = []
    weak_signals: list[str] = []
    checked_rule_count: int
    metrics: dict


class ThresholdSnapshot(CamelModel):
    """当前生效的监管阈值口径，报告页需展示以免误读。"""

    lpr_one_year_rate: float
    lpr_multiple: float
    judicial_protection_ceiling: float
    regulatory_cost_ceiling: float
    historical_hard_ceiling: float
    confidence_routing_threshold: float
