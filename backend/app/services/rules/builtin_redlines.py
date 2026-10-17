"""内置合规红线（数据形式，随启动写入 `red_line_rules`）。

引用依据的处理原则：
- `basis_refs` 只记录"法规名称 + 条号"作为**线索**，一律标记 `needs_verification=True`；
- 结论展示时，引用必须能在 `legal_provisions` 表中命中具体条文才算"已核验"，
  命中不到就降级为"引用未核验"并强制转人工复核；
- 本文件不写任何条文原文，避免把记忆中的表述当作权威文本喂给模型。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.enums import ClauseCategory, ContractKind, MetricOperator, RiskLevel


@dataclass(frozen=True, slots=True)
class BuiltinRedLine:
    code: str
    name: str
    category: str
    severity: str
    guidance_text: str
    suggestion_text: str
    applies_to: tuple[str, ...] = ()
    metric_key: str | None = None
    operator: str | None = None
    threshold_source: str = "static"
    threshold_value: float | None = None
    unit: str = "ratio"
    tolerance: float = 0.0
    secondary_threshold: float | None = None
    keyword_patterns: tuple[str, ...] = ()
    hard_block: bool = False
    llm_fallback: bool = True
    hit_confidence: float = 1.0
    basis_refs: tuple[dict, ...] = field(default_factory=tuple)
    priority: int = 100


_ALL: tuple[str, ...] = ()
_LOAN: tuple[str, ...] = (ContractKind.CONSUMER_LOAN.value, ContractKind.SMALL_LOAN.value)

BUILTIN_RED_LINES: tuple[BuiltinRedLine, ...] = (
    # ---------- 利率与费用：硬指标拦截 ----------
    BuiltinRedLine(
        code="RATE-001",
        name="综合年化融资成本超过监管红线",
        category=ClauseCategory.RATE_AND_FEE.value,
        severity=RiskLevel.HIGH.value,
        applies_to=_LOAN,
        metric_key="max_annual_cost_rate",
        operator=MetricOperator.GT.value,
        threshold_source="regulatory_cost_ceiling",
        hard_block=True,
        guidance_text=(
            "以合同中最不利口径的年化融资成本（含利息与各类费用折算）与监管红线比对，"
            "超出即定性为硬违规，模型结论不得推翻本判定。"
        ),
        suggestion_text=(
            "将各项利息与费用合并披露为单一年化口径并压回红线以内；"
            "对无法一次性清退的部分，应改为按实际占用天数计收并出具费用清单。"
        ),
        basis_refs=({"law_name": "银行保险机构消费者权益保护管理办法", "article_no": None},),
        priority=10,
    ),
    BuiltinRedLine(
        code="RATE-002",
        name="民间借贷利率超过司法保护上限",
        category=ClauseCategory.RATE_AND_FEE.value,
        severity=RiskLevel.HIGH.value,
        applies_to=(ContractKind.PRIVATE_LENDING.value,),
        metric_key="max_annual_cost_rate",
        operator=MetricOperator.GT.value,
        threshold_source="lpr_four_multiple",
        secondary_threshold=0.36,
        hard_block=True,
        guidance_text=(
            "民间借贷的利率司法保护上限为合同成立时一年期贷款市场报价利率（LPR）的四倍；"
            "持牌金融机构贷款不适用该上限，须改用监管综合成本红线口径。"
        ),
        suggestion_text="把约定利率降至成立时 4 倍 LPR 以内，并同步核查是否存在以费代息抬高实际成本的情形。",
        basis_refs=(
            {
                "law_name": "最高人民法院关于审理民间借贷案件适用法律若干问题的规定",
                # 现行有效文本是法释〔2020〕17号（2020年第二次修正，施行 2021-01-01）：
                # 4 倍 LPR 上限在第二十五条，第二十六条是"债权凭证载明金额认定为本金"。
                # 2020年8月第一次修正版（法释〔2020〕6号）里该规则才在第二十六条，已失效。
                "article_no": "第二十五条",
            },
        ),
        priority=10,
    ),
    BuiltinRedLine(
        code="RATE-003",
        name="月利率超过 3%（年化约 36%）",
        category=ClauseCategory.RATE_AND_FEE.value,
        severity=RiskLevel.HIGH.value,
        applies_to=(*_LOAN, ContractKind.PRIVATE_LENDING.value),
        metric_key="monthly_rate",
        operator=MetricOperator.GTE.value,
        threshold_value=0.03,
        unit="ratio",
        tolerance=0.0001,
        hard_block=True,
        guidance_text="月利率 3 分对应的复利年化已明显超出常规司法保护与监管口径，需按复利口径重新核算。",
        suggestion_text="下调月利率，并在合同中以年化形式重述利率，避免以“几分利”表述造成理解偏差。",
        basis_refs=({"law_name": "中华人民共和国民法典", "article_no": "第六百八十条"},),
        priority=20,
    ),
    BuiltinRedLine(
        code="RATE-004",
        name="未以年化形式明示融资成本",
        category=ClauseCategory.DISCLOSURE.value,
        severity=RiskLevel.HIGH.value,
        applies_to=_LOAN,
        metric_key="annual_cost_not_disclosed",
        operator=MetricOperator.EXISTS.value,
        hard_block=True,
        guidance_text=(
            "监管要求贷款产品以年化利率口径明示利率水平；仅表述“日息 X 元”“月费率 X%”"
            "而不同时披露年化口径，构成信息披露缺陷。"
        ),
        suggestion_text="在合同正文与还款计划页同步标注含全部费用的年化综合成本（APR/IRR 口径）。",
        basis_refs=({"law_name": "中国人民银行关于明示贷款年化利率的公告", "article_no": None},),
        priority=25,
    ),
    BuiltinRedLine(
        code="RATE-005",
        name="真实 IRR 显著高于名义利率",
        category=ClauseCategory.RATE_AND_FEE.value,
        severity=RiskLevel.HIGH.value,
        applies_to=_LOAN,
        metric_key="irr_annual_rate",
        operator=MetricOperator.GT.value,
        threshold_source="nominal_rate_multiple",
        threshold_value=1.5,
        guidance_text=(
            "按现金流求得的内部收益率超过名义利率的 1.5 倍，通常意味着存在以费代息、"
            "按初始本金计费或砍头息等隐性抬价安排。"
        ),
        suggestion_text="重算并统一各项收费口径，将费用并入利率披露；对按初始本金计费的费用改为按余额计费。",
        basis_refs=({"law_name": "银行保险机构消费者权益保护管理办法", "article_no": None},),
        priority=30,
    ),
    BuiltinRedLine(
        code="FEE-001",
        name="本金被预先扣除利息或费用",
        category=ClauseCategory.RATE_AND_FEE.value,
        severity=RiskLevel.HIGH.value,
        applies_to=(*_LOAN, ContractKind.PRIVATE_LENDING.value),
        metric_key="disbursement_shortfall_ratio",
        operator=MetricOperator.GT.value,
        threshold_value=0.005,
        unit="ratio",
        keyword_patterns=(
            r"(?:预先|提前|从[^。]{0,12}(?:本金|借款金额)中)[^。]{0,12}(?:扣除|扣收)[^。]{0,16}(?:利息|手续费|服务费)",
        ),
        hard_block=True,
        guidance_text="预先在本金中扣除利息或费用的，应按实际借款数额返还借款并计算利息。",
        suggestion_text="改为足额放款、按期计收利息；确需扣除的费用单独列示并重算实际年化成本。",
        basis_refs=({"law_name": "中华人民共和国民法典", "article_no": "第六百七十条"},),
        priority=15,
    ),
    BuiltinRedLine(
        code="FEE-002",
        name="多名称费用另行收取且未合并披露",
        category=ClauseCategory.RATE_AND_FEE.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=_LOAN,
        keyword_patterns=(
            r"(?:服务费|担保费|咨询费|管理费|会员费|渠道费)[^。]{0,20}(?:另行|单独|一次性)收取",
        ),
        hit_confidence=0.75,
        guidance_text="以多种名目分别收费会分散消费者的成本感知，需要合并核算综合年化成本后再判定。",
        suggestion_text="合并披露全部费用并给出年化口径，删除重复收取的名目。",
        basis_refs=({"law_name": "银行保险机构消费者权益保护管理办法", "article_no": None},),
        priority=40,
    ),
    # ---------- 格式条款与消费者保护 ----------
    BuiltinRedLine(
        code="PROT-001",
        name="约定最终解释权归放款方",
        category=ClauseCategory.CONSUMER_PROTECTION.value,
        severity=RiskLevel.HIGH.value,
        applies_to=_ALL,
        metric_key="final_interpretation",
        operator=MetricOperator.EXISTS.value,
        hard_block=True,
        guidance_text="经营者不得以格式条款作出排除或限制消费者权利、减轻自身责任的规定。",
        suggestion_text="删除最终解释权表述，改为“合同条款理解有争议时，按通常理解解释；有两种以上解释的，作不利于提供格式条款一方的解释”。",
        basis_refs=({"law_name": "中华人民共和国消费者权益保护法", "article_no": "第二十六条"},),
        priority=18,
    ),
    BuiltinRedLine(
        code="PROT-002",
        name="要求消费者预先放弃抗辩或诉讼权利",
        category=ClauseCategory.CONSUMER_PROTECTION.value,
        severity=RiskLevel.HIGH.value,
        applies_to=_ALL,
        metric_key="waive_procedural_rights",
        operator=MetricOperator.EXISTS.value,
        hard_block=True,
        guidance_text="预先放弃程序性权利的格式条款多被认定为无效，且属于监管重点整治的霸王条款。",
        suggestion_text="删除放弃抗辩、放弃起诉等表述，保留双方平等的争议解决路径。",
        basis_refs=(
            {"law_name": "中华人民共和国民法典", "article_no": "第四百九十七条"},
            {"law_name": "中华人民共和国消费者权益保护法", "article_no": "第二十六条"},
        ),
        priority=18,
    ),
    BuiltinRedLine(
        code="PROT-003",
        name="可单方随时调整利率或费用",
        category=ClauseCategory.CONSUMER_PROTECTION.value,
        severity=RiskLevel.HIGH.value,
        applies_to=(*_LOAN, ContractKind.PARTNERSHIP.value),
        metric_key="unilateral_price_change",
        operator=MetricOperator.EXISTS.value,
        hit_confidence=0.85,
        guidance_text="价格条款由一方随时调整而对方无异议机制，构成权利义务显著失衡。",
        suggestion_text="改为固定利率或明确、可验证的调整公式，并赋予消费者在调整时的提前还款或解约权。",
        basis_refs=({"law_name": "中华人民共和国民法典", "article_no": "第四百九十七条"},),
        priority=35,
    ),
    BuiltinRedLine(
        code="PROT-004",
        name="概括性扣划授权",
        category=ClauseCategory.CONSUMER_PROTECTION.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=_LOAN,
        metric_key="blank_deduction_authorization",
        operator=MetricOperator.EXISTS.value,
        hit_confidence=0.75,
        guidance_text="不绑定金额、期限与通知程序的划扣授权，需由模型与人工判断其必要性与可撤销性。",
        suggestion_text="把授权限定为具体期次与具体金额，约定扣划前通知义务与撤回授权的路径。",
        basis_refs=({"law_name": "银行保险机构消费者权益保护管理办法", "article_no": None},),
        priority=50,
    ),
    BuiltinRedLine(
        code="PROT-005",
        name="强制搭售保险或增值服务",
        category=ClauseCategory.TIED_PRODUCT.value,
        severity=RiskLevel.HIGH.value,
        applies_to=_LOAN,
        metric_key="mandatory_insurance",
        operator=MetricOperator.EXISTS.value,
        hard_block=True,
        guidance_text="不得以购买保险、增值服务作为发放贷款的前提条件，剥夺消费者自主选择权。",
        suggestion_text="删除强制投保前提，改为可选并在合同中单独签署知情同意。",
        basis_refs=({"law_name": "银行保险机构消费者权益保护管理办法", "article_no": None},),
        priority=22,
    ),
    # ---------- 个人信息与数据 ----------
    BuiltinRedLine(
        code="INFO-001",
        name="向第三方提供个人信息未取得单独同意",
        category=ClauseCategory.PERSONAL_INFO.value,
        severity=RiskLevel.HIGH.value,
        applies_to=_ALL,
        metric_key="broad_info_sharing",
        operator=MetricOperator.EXISTS.value,
        hit_confidence=0.85,
        guidance_text="向其他个人信息处理者提供个人信息，应向个人告知接收方名称、处理目的、方式与信息种类并取得单独同意。",
        suggestion_text="拆分授权条款，逐项列明接收方、目的与种类，并以独立勾选方式取得单独同意。",
        basis_refs=({"law_name": "中华人民共和国个人信息保护法", "article_no": "第二十三条"},),
        priority=28,
    ),
    # ---------- 违约、催收与加速到期 ----------
    BuiltinRedLine(
        code="BRCH-001",
        name="违约金比例过高",
        category=ClauseCategory.BREACH_PENALTY.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=_ALL,
        metric_key="breach_penalty_ratio",
        operator=MetricOperator.GT.value,
        threshold_value=0.3,
        unit="ratio",
        guidance_text="约定违约金过分高于造成的损失时，当事人可请求适当减少；30% 常作为过高的参照线。",
        suggestion_text="下调违约金比例并以实际损失为基础约定计算方式，避免与罚息重复计收。",
        basis_refs=({"law_name": "中华人民共和国民法典", "article_no": "第五百八十五条"},),
        priority=45,
    ),
    BuiltinRedLine(
        code="BRCH-002",
        name="罚息与费用重复计收",
        category=ClauseCategory.BREACH_PENALTY.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=_LOAN,
        keyword_patterns=(
            # 费用名目与"计收"之间若出现否定词（不/未/无/毋），该句是合规表述而非违规。
            # 原样写 [^。]{0,16} 会把"罚息与违约金不重复计收"读成命中，造成误报。
            r"(?:罚息|逾期利息)[^。]{0,24}"
            r"(?:违约金|滞纳金|催收费|管理费)"
            r"(?:(?!不|未|无|毋)[^。]){0,16}"
            r"(?:并|同时|另行)?(?:计收|收取|支付)",
        ),
        hit_confidence=0.8,
        guidance_text="逾期阶段同时计收罚息、违约金与各类费用时，合计口径仍受利率上限约束。",
        suggestion_text="明确逾期成本合计上限，超出部分不予主张。",
        basis_refs=({"law_name": "中华人民共和国民法典", "article_no": "第五百八十五条"},),
        priority=55,
    ),
    BuiltinRedLine(
        code="BRCH-003",
        name="委托第三方催收缺少合规约束",
        category=ClauseCategory.BREACH_PENALTY.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=(*_LOAN, ContractKind.PARTNERSHIP.value),
        metric_key="third_party_collection",
        operator=MetricOperator.EXISTS.value,
        hit_confidence=0.75,
        guidance_text="委外催收的行为责任由金融机构承担，需约定禁止行为、信息边界与投诉处理机制。",
        suggestion_text="补充催收行为规范、可联系范围、禁止联系无关第三人及投诉响应时限。",
        basis_refs=({"law_name": "银行保险机构消费者权益保护管理办法", "article_no": None},),
        priority=60,
    ),
    BuiltinRedLine(
        code="BRCH-004",
        name="提前到期触发条件宽泛",
        category=ClauseCategory.BREACH_PENALTY.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=(*_LOAN, ContractKind.GUARANTEE.value),
        metric_key="acceleration_broad",
        operator=MetricOperator.EXISTS.value,
        hit_confidence=0.75,
        guidance_text="未明确列举触发情形的加速到期条款，可能被认定为赋予一方任意宣布到期的权利。",
        suggestion_text="以封闭式列举写明触发情形、催告程序与宽限期，并给予提前还款与补正机会。",
        basis_refs=({"law_name": "中华人民共和国民法典", "article_no": "第四百九十七条"},),
        priority=65,
    ),
    # ---------- 续贷与展期 ----------
    BuiltinRedLine(
        code="ROLL-001",
        name="自动续贷或借新还旧",
        category=ClauseCategory.REPAYMENT.value,
        severity=RiskLevel.HIGH.value,
        applies_to=(*_LOAN, ContractKind.PARTNERSHIP.value),
        metric_key="auto_rollover",
        operator=MetricOperator.EXISTS.value,
        hit_confidence=0.85,
        guidance_text="自动续贷/借新还旧会掩盖真实逾期状态并可能重复计收费用与利息。",
        suggestion_text="改为需借款人逐次确认的续贷流程，明确续贷时费用不再重复收取。",
        basis_refs=({"law_name": "个人贷款管理办法", "article_no": None},),
        priority=42,
    ),
    # ---------- 担保与合作协议 ----------
    BuiltinRedLine(
        code="GUAR-001",
        name="保证金或首付比例偏高",
        category=ClauseCategory.COLLATERAL_GUARANTEE.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=(ContractKind.GUARANTEE.value, ContractKind.PARTNERSHIP.value),
        metric_key="guarantee_deposit_ratio",
        operator=MetricOperator.GT.value,
        threshold_value=0.2,
        unit="ratio",
        guidance_text="过高的保证金/首付比例会实质提高消费者融资成本并放大资金占用争议。",
        suggestion_text="下调比例，并写明释放条件、计息方式与退还时限。",
        basis_refs=({"law_name": "融资担保公司监督管理条例", "article_no": None},),
        priority=70,
    ),
    BuiltinRedLine(
        code="GUAR-002",
        name="担保责任范围未以显著方式提示",
        category=ClauseCategory.DISCLOSURE.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=(ContractKind.GUARANTEE.value, ContractKind.CONSUMER_LOAN.value),
        keyword_patterns=(
            r"(?:承担连带(?:责任|保证)|全额担保|担保范围包括)[^。]{0,40}(?:未|不)(?:另行|单独)?(?:提示|告知|说明)",
        ),
        hit_confidence=0.7,
        guidance_text="与消费者有重大利害关系的内容须以显著方式提示并按其要求说明。",
        suggestion_text="对担保范围、代偿触发与追偿路径设置单独加粗提示条款与签署确认栏。",
        basis_refs=({"law_name": "中华人民共和国民法典", "article_no": "第四百九十六条"},),
        priority=75,
    ),
    # ---------- 争议解决 ----------
    BuiltinRedLine(
        code="DISP-001",
        name="管辖约定偏向放款方所在地",
        category=ClauseCategory.DISPUTE.value,
        severity=RiskLevel.MEDIUM.value,
        applies_to=_ALL,
        metric_key="lender_home_jurisdiction",
        operator=MetricOperator.EXISTS.value,
        hit_confidence=0.8,
        guidance_text="由放款方住所地法院管辖会显著抬高消费者维权成本，格式条款中易被认定无效。",
        suggestion_text="改为被告住所地或合同签订地/履行地管辖，或约定双方共同认可的仲裁机构。",
        basis_refs=({"law_name": "中华人民共和国民事诉讼法", "article_no": "第三十五条"},),
        priority=80,
    ),
    # ---------- 主体与资质 ----------
    BuiltinRedLine(
        code="SUBJ-001",
        name="未载明放贷主体与许可资质",
        category=ClauseCategory.SUBJECT_AUTHORITY.value,
        severity=RiskLevel.HIGH.value,
        applies_to=(*_LOAN, ContractKind.PARTNERSHIP.value),
        keyword_patterns=(
            r"(?:出借人|贷款人|资金方)[:：]?\s*(?:[X＊*]{3,}|待补充|以实际放款机构为准|另行告知)",
        ),
        hit_confidence=0.7,
        guidance_text="实际放款主体不明会使消费者无法确定债权人身份与救济对象，助贷模式下尤为敏感。",
        suggestion_text="在合同中明确填写实际放款机构全称及其许可资质信息，禁止使用占位或“以放款为准”。",
        basis_refs=({"law_name": "商业银行互联网贷款管理暂行办法", "article_no": None},),
        priority=38,
    ),
)


def as_rule_rows() -> list[dict]:
    """转换为可直接插入 `RedLineRule` 的字段字典。"""
    rows: list[dict] = []
    for rule in BUILTIN_RED_LINES:
        rows.append(
            {
                "code": rule.code,
                "name": rule.name,
                "category": rule.category,
                "severity": rule.severity,
                "applies_to": list(rule.applies_to),
                "metric_key": rule.metric_key,
                "operator": rule.operator,
                "threshold_source": rule.threshold_source,
                "threshold_value": rule.threshold_value,
                "unit": rule.unit,
                "tolerance": rule.tolerance,
                "secondary_threshold": rule.secondary_threshold,
                "keyword_patterns": list(rule.keyword_patterns),
                "hard_block": rule.hard_block,
                "llm_fallback": rule.llm_fallback,
                "hit_confidence": rule.hit_confidence,
                "basis_refs": [dict(ref) for ref in rule.basis_refs],
                "guidance_text": rule.guidance_text,
                "suggestion_text": rule.suggestion_text,
                "priority": rule.priority,
                "is_builtin": True,
                "is_active": True,
            }
        )
    return rows
