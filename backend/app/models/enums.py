"""领域枚举。

数据库中以字符串存储（避免新增取值时被迫执行 DDL 变更），
校验统一交给 Pydantic，路由层不接受枚举外的原始字符串。
"""

from __future__ import annotations

from enum import StrEnum


class ContractStatus(StrEnum):
    UPLOADED = "uploaded"      # 已上传，待解析
    PARSING = "parsing"        # 正在抽取文本
    PARSED = "parsed"          # 解析完成，待审查
    REVIEWING = "reviewing"    # 审查中
    REVIEWED = "reviewed"      # 审查完成
    FAILED = "failed"          # 解析或审查失败


class ReviewStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RiskLevel(StrEnum):
    """风险等级，同时用于排序与评分。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"

    @property
    def weight(self) -> int:
        return {"high": 3, "medium": 2, "low": 1, "none": 0}[self.value]


class ClauseCategory(StrEnum):
    """信贷合同的条款分类，也是红线库与检索过滤的分组维度。"""

    RATE_AND_FEE = "rate_and_fee"            # 利率、费用与综合融资成本
    REPAYMENT = "repayment"                  # 还款方式、期限与提前还款
    COLLATERAL_GUARANTEE = "collateral_guarantee"  # 担保、抵押与保证金
    BREACH_PENALTY = "breach_penalty"        # 违约、罚息与催收
    CONSUMER_PROTECTION = "consumer_protection"    # 消费者保护与格式条款
    PERSONAL_INFO = "personal_info"          # 个人信息与数据授权
    TIED_PRODUCT = "tied_product"            # 搭售、保险与增值服务
    DISPUTE = "dispute"                      # 争议解决与管辖
    SUBJECT_AUTHORITY = "subject_authority"  # 主体资格与放贷资质
    DISCLOSURE = "disclosure"                # 信息披露与风险提示
    OTHER = "other"


class FindingSource(StrEnum):
    """结论来源，双通道交叉校验的核心标记。"""

    RULE = "rule"      # 规则引擎硬命中，可复现、可解释
    LLM = "llm"        # 仅 LLM + RAG 判定
    BOTH = "both"      # 两条通道结论一致，可信度最高


class CrossCheckStatus(StrEnum):
    """交叉校验结果。"""

    CONSISTENT = "consistent"    # 双通道一致
    RULE_OVERRIDES = "rule_overrides"  # 冲突时以规则引擎为准
    LLM_ONLY = "llm_only"        # 规则无法判定，纯模型判断
    RULE_ONLY = "rule_only"      # 模型漏报，规则命中


class HumanReviewStatus(StrEnum):
    """法务复核状态，构成数据飞轮的标注闭环。"""

    NOT_REQUIRED = "not_required"  # 置信度达标且为规则硬命中
    PENDING = "pending"            # 已进入复核队列
    APPROVED = "approved"          # 确认成立
    REJECTED = "rejected"          # 误报
    ADJUSTED = "adjusted"          # 成立但等级/建议需调整


class HumanReviewReason(StrEnum):
    """转人工复核的触发原因，也是复核队列的过滤维度。"""

    LOW_CONFIDENCE = "low_confidence"          # 置信度未达阈值
    RULE_LLM_CONFLICT = "rule_llm_conflict"    # 双通道结论冲突
    JUDGE_DISPUTED = "judge_disputed"          # 复核模型认为结论不成立或置信度不足
    HIGH_RISK_MANDATORY = "high_risk_mandatory"  # 高风险强制人工确认
    UNVERIFIED_CITATION = "unverified_citation"  # 引用的法条在库中找不到
    METRIC_MISSING = "metric_missing"          # 关键数值无法抽取，需人工核对
    MANUAL = "manual"                          # 人工主动挂起


class KnowledgeDocType(StrEnum):
    """知识库文档类型。"""

    LAW = "law"                          # 法律
    ADMIN_REGULATION = "admin_regulation"  # 行政法规
    DEPARTMENT_RULE = "department_rule"    # 部门规章（监管规定）
    JUDICIAL_INTERPRETATION = "judicial_interpretation"  # 司法解释
    REGULATORY_GUIDE = "regulatory_guide"  # 监管指引/通知
    ENFORCEMENT_CASE = "enforcement_case"  # 监管罚单
    INTERNAL_REDLINE = "internal_redline"  # 内部合规红线


class CitationVerification(StrEnum):
    """结论级引用核验状态（`ReviewFinding.verification_status`）。

    核验事实原本只藏在 `citations` JSON 里，SQL 层无法聚合，
    算"引用已核验率"（GEN-06）就得把每份审查的 JSON 拉回内存解析。
    这里把它升成一个可分组、可加索引的结论级标签。
    """

    NONE = "none"                    # 结论未给出任何法规依据
    VERIFIED = "verified"            # 全部引用都命中已导入原文的条文
    PARTIAL = "partial"              # 部分已核验、部分未核验
    PLACEHOLDER = "placeholder"      # 全部只有占位登记（原文未导入）
    NOT_IN_KB = "not_in_kb"          # 全部未收录：几乎必然是模型编造


class CitationVerifyCode(StrEnum):
    """单条引用的核验结论编码，写进 `citations[].verifyCode`。

    原先"未收录"与"仅占位"都只体现在中文 `verifyReason` 文本里，
    靠文案区分核验失败原因迟早被改字句改坏；评测要按类别统计，必须有稳定编码。
    """

    OK = "ok"                        # 已核验且有权威原文
    PLACEHOLDER = "placeholder"      # 库里有登记行，但内容是占位文本
    NOT_IN_KB = "not_in_kb"          # 库里查不到这部法规/这个条号


class LocateMethod(StrEnum):
    """结论回连合同原文所使用的手段（`ReviewFinding.locate_method`）。

    定位失败率与"靠哪种手段定位上"是两件事：只靠去空白匹配定位上的结论，
    在 PDF 抽取质量差时更可能错位，评测 span 指标时必须能区分。
    """

    EXACT = "exact"                  # 原文精确子串命中
    NORMALIZED = "normalized"        # 去空白后命中并回映射偏移
    SEGMENT_FALLBACK = "segment_fallback"  # 只归到模型指认的条款，非精确
    EVIDENCE = "evidence"            # 规则通道自带证据偏移
    FAILED = "failed"                # 完全没定位上（前端高亮落空）


class ModelSlot(StrEnum):
    """模型调用属于哪个槽位，写进 `review_llm_calls.slot`。"""

    REVIEW = "review"  # 主审通道（环节3 合规规则匹配 + 环节4 风险识别与定性）
    JUDGE = "judge"    # 复核通道（环节7 结论复核）


class MetricOperator(StrEnum):
    """红线规则的比较符。"""

    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"
    EXISTS = "exists"
    MISSING = "missing"


class ContractKind(StrEnum):
    """合同种类，决定加载哪一组红线。"""

    CONSUMER_LOAN = "consumer_loan"      # 消费信贷
    SMALL_LOAN = "small_loan"            # 小额贷款
    PRIVATE_LENDING = "private_lending"  # 民间借贷（适用 4 倍 LPR 司法保护上限）
    GUARANTEE = "guarantee"              # 担保/抵押协议
    PARTNERSHIP = "partnership"          # 助贷/联合贷等非标合作协议
    UNKNOWN = "unknown"


#: 风险折算权重：risk_score = 100 - Σpenalty，下限 0
RISK_SCORE_PENALTY: dict[RiskLevel, int] = {
    RiskLevel.HIGH: 18,
    RiskLevel.MEDIUM: 8,
    RiskLevel.LOW: 2,
    RiskLevel.NONE: 0,
}

#: 置信度路由阈值：低于该值的 LLM 结论强制转人工法务复核
CONFIDENCE_ROUTING_THRESHOLD = 0.80
