"""合规红线规则库与规则引擎。"""

from app.services.rules.builtin_redlines import BUILTIN_RED_LINES, as_rule_rows
from app.services.rules.engine import RuleEngine, RuleEvaluation, RuleHit
from app.services.rules.metrics import Evidence, MetricReading, MetricSet, extract_metrics
from app.services.rules.seed import (
    ensure_builtin_rules,
    load_active_rules,
    rules_for_prompt,
)

__all__ = [
    "BUILTIN_RED_LINES",
    "Evidence",
    "MetricReading",
    "MetricSet",
    "RuleEngine",
    "RuleEvaluation",
    "RuleHit",
    "as_rule_rows",
    "ensure_builtin_rules",
    "extract_metrics",
    "load_active_rules",
    "rules_for_prompt",
]
