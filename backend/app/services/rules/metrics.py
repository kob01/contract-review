"""合同数值指标与文本特征抽取（确定性、可复现）。

这一层不放任何"判断"，只负责把合同里的数字和危险表述抽出来，
并保留出处（原文片段 + 全文偏移 + 页码 + 计算过程）。

为什么利率必须由代码算而不是交给模型：
- "月利率 3 分、按名义本金收取手续费、分 12 期等本等费"这类写法，
  真实年化接近 60%+，模型极易只报出表面的 36%；
- 数值判定必须可回归测试，出错要能定位到一条具体的正则或现金流模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.parser.extractor import ExtractedDocument

# ---------- 数据结构 ----------


@dataclass(slots=True)
class Evidence:
    """一次抽取命中。"""

    key: str
    label: str
    kind: str                     # metric | flag
    value: float | None = None    # 统一用小数表示比率（0.36 = 36%）
    display: str = ""
    derivation: str = ""          # 计算过程，前端直接展示，便于人工核对
    excerpt: str = ""
    char_start: int | None = None
    char_end: int | None = None
    segment_index: int | None = None
    page_no: int | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "value": self.value,
            "display": self.display,
            "derivation": self.derivation,
            "excerpt": self.excerpt,
            "charStart": self.char_start,
            "charEnd": self.char_end,
            "segmentIndex": self.segment_index,
            "pageNo": self.page_no,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> Evidence:
        return cls(
            key=payload.get("key", ""),
            label=payload.get("label", ""),
            kind=payload.get("kind", "metric"),
            value=payload.get("value"),
            display=payload.get("display", ""),
            derivation=payload.get("derivation", ""),
            excerpt=payload.get("excerpt", ""),
            char_start=payload.get("charStart"),
            char_end=payload.get("charEnd"),
            segment_index=payload.get("segmentIndex"),
            page_no=payload.get("pageNo"),
        )


@dataclass(slots=True)
class MetricSet:
    """一份合同的全部抽取结果。"""

    evidences: list[Evidence] = field(default_factory=list)
    #: 派生标量（如“最不利年化口径”），不对应单一原文片段
    derived: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def by_key(self, key: str) -> list[Evidence]:
        return [item for item in self.evidences if item.key == key and item.value is not None]

    def max_of(self, key: str) -> Evidence | None:
        items = self.by_key(key)
        return max(items, key=lambda item: item.value or 0.0) if items else None

    def value_of(self, key: str) -> MetricReading | None:
        """按优先级取值：派生标量 → 数值指标 → 文本特征命中。

        同一个指标在合同里可能出现多次（如“年利率 24%”与“日息万分之五”并存），
        取最不利值参与红线比对，避免低估风险。
        """
        if key in self.derived:
            return MetricReading(key=key, value=self.derived[key])

        evidence = self.max_of(key)
        if evidence is not None and evidence.value is not None:
            return MetricReading(key=key, value=evidence.value, evidence=evidence)

        flag = self.first_flag(key)
        if flag is not None:
            return MetricReading(key=key, value=1.0, evidence=flag, is_flag=True)
        return None

    def has_flag(self, key: str) -> bool:
        return any(item.key == key and item.kind == "flag" for item in self.evidences)

    def first_flag(self, key: str) -> Evidence | None:
        for item in self.evidences:
            if item.key == key and item.kind == "flag":
                return item
        return None

    def to_dict(self) -> dict:
        return {
            "evidences": [item.to_dict() for item in self.evidences],
            "derived": self.derived,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> MetricSet:
        """从 `Contract.extracted_metrics` 重建，避免每次审查重跑抽取。"""
        payload = payload or {}
        return cls(
            evidences=[Evidence.from_dict(item) for item in payload.get("evidences", [])],
            derived={str(key): float(value) for key, value in (payload.get("derived") or {}).items()},
            warnings=list(payload.get("warnings") or []),
        )


@dataclass(slots=True)
class MetricReading:
    """一次指标读取结果（值 + 出处）。"""

    key: str
    value: float
    evidence: Evidence | None = None
    is_flag: bool = False


# ---------- 数值模式 ----------

_NUM = r"([0-9]+(?:\.[0-9]+)?)"
#: 中文数字与阿拉伯数字都可能出现（“日息万分之五”是主流写法）
_NUM_OR_CN = r"([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十百千]{1,6}(?:\.[0-9]+)?)"
_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_FEE_WORDS = r"(?:服务费|担保费|管理费|咨询费|手续费|会员费|渠道费|风险保证金)"

#: 各直述型利率正则，单独命名以便长模式折行（中文正则单行极易超行宽）
P_NOMINAL_ANNUAL = rf"(?:年利率|年化利率|年息)\s*(?:为|不超过|不低于)?\s*{_NUM}\s*%"
P_COMPREHENSIVE_ANNUAL = (
    rf"(?:综合年化(?:资金|融资)?成本|实际年化利率|IRR|年化利率[（(]含各类费用[)）])"
    rf"\s*(?:为|不超过)?\s*{_NUM}\s*%"
)
P_MONTHLY = rf"月(?:利率|息)\s*(?:为|按)?\s*{_NUM}\s*%"
P_DAILY = rf"日(?:利率|息|利息)\s*(?:为|按|万分之)?\s*{_NUM}\s*%"
P_OVERDUE_UPLIFT = rf"(?:罚息|逾期利息)[^0-9。]{{0,20}}上浮\s*{_NUM}\s*%"
P_BREACH_PENALTY = rf"违约金[^0-9。]{{0,20}}{_NUM}\s*%"
P_PREPAYMENT_PENALTY = rf"提前还款[^。]{{0,30}}{_NUM}\s*%"
P_GUARANTEE_DEPOSIT = rf"保证金[^0-9。]{{0,20}}{_NUM}\s*%"

#: (key, label, pattern, 单位口径)
_RATE_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    ("nominal_annual_rate", "名义年利率", P_NOMINAL_ANNUAL, "percent"),
    ("comprehensive_annual_rate", "综合年化融资成本", P_COMPREHENSIVE_ANNUAL, "percent"),
    ("monthly_rate", "月利率", P_MONTHLY, "percent"),
    ("daily_rate", "日利率", P_DAILY, "percent"),
    ("overdue_uplift_rate", "罚息上浮比例", P_OVERDUE_UPLIFT, "percent"),
    ("breach_penalty_ratio", "违约金比例", P_BREACH_PENALTY, "percent"),
    ("prepayment_penalty_ratio", "提前还款违约金比例", P_PREPAYMENT_PENALTY, "percent"),
    ("guarantee_deposit_ratio", "保证金比例", P_GUARANTEE_DEPOSIT, "percent"),
)

#: 日息"万分之X"，消费贷合同最常见的写法（X 可以是中文数字）
_WFZH_PATTERN = rf"(?:每日|按日|日息|日利率)[^0-9。]{{0,12}}万分之\s*{_NUM_OR_CN}"
_PERMILLE_DAILY = rf"日(?:利率|息)\s*(?:为|按)?\s*{_NUM}\s*‰"

#: 按月收取的费用率：每月按本金的 X% 收取服务费
_MONTHLY_FEE_PATTERN = (
    rf"(?:每月|按月|每期)[^。]{{0,20}}?(?:本金|借款金额|剩余本金)[^。]{{0,12}}?{_NUM}\s*%"
    rf"[^。]{{0,16}}?{_FEE_WORDS}"
)
_FEE_MONTHLY_REVERSE = (
    rf"{_FEE_WORDS}[^。]{{0,20}}?(?:每月|按月|每期)[^。]{{0,16}}?{_NUM}\s*%"
)

_PRINCIPAL_PATTERN = (
    r"(?:借款(?:金额|本金)|贷款(?:金额|本金)|授信额度)"
    r"\s*(?:为|即)?\s*[:：]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|元)"
)
_TERM_PATTERN = r"(?:借款|贷款)期限\s*(?:为|即)?\s*[:：]?\s*([0-9]+)\s*(个月|月|天|日|年)"
_INSTALLMENT_PATTERN = (
    r"(?:每期|每月)(?:还款|偿付|支付)(?:金额|额)?"
    r"\s*(?:为|即)?\s*[:：]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*元"
)
#: 实际到账金额，与本金不一致时意味着存在预扣（砍头息）
_DISBURSEMENT_PATTERN = (
    r"(?:实际(?:放款|支付|发放)|到账)\s*(?:金额)?\s*(?:为|即)?\s*[:：]?\s*"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|元)"
)

#: (flag_key, label, pattern, 判定说明)
_FLAG_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    (
        "blank_deduction_authorization",
        "概括性扣划授权",
        r"(?:授权|委托)[^。]{0,24}(?:直接|自行)(?:扣划|扣除|划扣)[^。]{0,24}(?:无需[^。]{0,10}(?:通知|同意)|不[^。]{0,6}(?:另行)?(?:通知|确认))",
        "扣款授权未绑定具体金额与到期日，属于加重消费者责任、排除其主要权利的格式条款。",
    ),
    (
        "waive_procedural_rights",
        "放弃抗辩或诉讼权利",
        r"(?:放弃|不得|无权)[^。]{0,12}(?:抗辩|异议|申诉|起诉|应诉|申请(?:仲裁|回避))",
        "预先要求消费者放弃程序性权利，实践中多被认定为无效格式条款。",
    ),
    (
        "unilateral_price_change",
        "单方调整费率",
        r"(?:有权|可|保留[^。]{0,8}权利)[^。]{0,16}(?:单方|自行|随时)[^。]{0,12}(?:调整|变更|修改|上调)[^。]{0,16}(?:利率|费率|费用|标准|条款)",
        "价格条款可由一方随时变更而消费者无异议机制，构成显著失衡。",
    ),
    (
        "final_interpretation",
        "最终解释权条款",
        r"(?:最终解释权|解释权)[^。]{0,12}(?:归|属于|由)[^。]{0,16}(?:本公司|我方|贷款人|甲方)",
        "“最终解释权归出借方”属于典型的霸王条款表述。",
    ),
    (
        "broad_info_sharing",
        "概括性个人信息共享",
        r"(?:向|提供给|共享给)[^。]{0,16}(?:第三方|关联公司|合作机构)[^。]{0,16}(?:提供|共享|披露)[^。]{0,12}(?:个人信息|信用信息|征信)",
        "未区分必要范围、未取得单独同意即向第三方提供个人信息，超出最小必要原则。",
    ),
    (
        "mandatory_insurance",
        "强制搭售保险",
        r"(?:必须|应当|须|以[^。]{0,8}为前提)[^。]{0,12}(?:投保|购买(?:保险|意外险|保证险)|附加[^。]{0,8}保险)",
        "把购买保险设为放款前提，属于监管明令制止的搭售行为。",
    ),
    (
        "auto_rollover",
        "自动续贷或借新还旧",
        r"(?:自动续贷|自动展期|借新还旧|自动重新发放贷款)",
        "自动续贷/借新还旧会掩盖真实逾期情况并重复计收费用。",
    ),
    (
        "lender_home_jurisdiction",
        "偏向性管辖约定",
        r"(?:(?:管辖|诉讼)[^。]{0,24}(?:贷款人|出借人|甲方|本公司)(?:所在地|住所地|注册地)"
        r"|(?:贷款人|出借人|甲方|本公司)(?:所在地|住所地|注册地)[^。]{0,16}管辖)",
        "由放款方住所地法院管辖会显著抬高消费者维权成本。",
    ),
    (
        "acceleration_broad",
        "宽泛的提前到期条款",
        r"(?:贷款人|甲方|本公司)[^。]{0,20}(?:有权|可)[^。]{0,12}(?:宣布|认定)[^。]{0,12}(?:全部|提前)[^。]{0,10}(?:到期|清偿)",
        "提前到期触发条件若不明确列举，容易演变为任意加速到期。",
    ),
    (
        "head_interest_deduction",
        "预先扣除利息或费用",
        r"(?:从[^。]{0,12}(?:本金|借款金额|放款金额)中[^。]{0,12}(?:扣除|扣收|扣减)"
        r"|(?:预先|提前)(?:在本金中)?(?:扣除|扣收|扣减)[^。]{0,16}(?:利息|手续费|服务费|管理费|咨询费)"
        r"|(?:利息|手续费|服务费|管理费|咨询费)[^。]{0,16}(?:从本金中|预先)(?:扣除|扣收))",
        "预先在本金中扣除利息或费用的，应按实际借款数额返还借款并计算利息。",
    ),
    (
        "third_party_collection",
        "委托第三方催收",
        r"(?:委托|交由)[^。]{0,16}(?:第三方|催收公司|外包机构)[^。]{0,12}催收",
        "第三方催收的行为责任仍由放款方承担，需要约定禁止行为与投诉处理机制。",
    ),
)

_IRR_TOLERANCE = 1e-7

#: 对外导出的特征表别名（供离线 Mock 与测试复用，避免两处维护同一批正则）
FLAG_PATTERNS = _FLAG_PATTERNS


def _annualize_monthly(monthly_ratio: float) -> float:
    """月费率按复利折算年化。"""
    return (1.0 + monthly_ratio) ** 12 - 1.0


def _solve_irr(cashflows: list[float], low: float = -0.9999, high: float = 10.0) -> float | None:
    """对月度现金流用二分法求周期 IRR，返回月度利率（小数）。

    找不到符号变化时（例如全为正现金流）返回 None，交由上层提示"数据不足"。
    """
    if len(cashflows) < 2:
        return None

    def npv(rate: float) -> float:
        return sum(cf / (1.0 + rate) ** period for period, cf in enumerate(cashflows))

    lo, hi = low, high
    lo_v, hi_v = npv(lo), npv(hi)
    if lo_v * hi_v > 0:
        return None

    for _ in range(200):
        mid = (lo + hi) / 2
        value = npv(mid)
        if abs(value) < _IRR_TOLERANCE:
            return mid
        if lo_v * value < 0:
            hi, hi_v = mid, value
        else:
            lo, lo_v = mid, value
    return (lo + hi) / 2


def _parse_number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _locate(segments: list[dict], full_text: str, start: int, end: int) -> dict:
    from app.services.parser.segmenter import locate_excerpt

    match_text = full_text[start:end]
    position = locate_excerpt(segments, full_text, match_text) or {
        "segment_index": None,
        "char_start": start,
        "char_end": end,
        "page_no": 1,
    }
    return position


def _add(
    result: MetricSet,
    *,
    key: str,
    label: str,
    kind: str,
    full_text: str,
    segments: list[dict],
    start: int,
    end: int,
    value: float | None = None,
    display: str = "",
    derivation: str = "",
) -> None:
    position = _locate(segments, full_text, start, end)
    result.evidences.append(
        Evidence(
            key=key,
            label=label,
            kind=kind,
            value=value,
            display=display,
            derivation=derivation,
            excerpt=re.sub(r"\s+", " ", full_text[start:end]).strip(),
            char_start=start,
            char_end=end,
            segment_index=position["segment_index"],
            page_no=position["page_no"],
        )
    )


_KEY_TO_LABEL = {
    "nominal_annual_rate": "名义年利率",
    "comprehensive_annual_rate": "综合年化融资成本",
    "monthly_rate": "月利率",
    "daily_rate": "日利率",
    "monthly_fee_rate": "月费率（含各类服务费）",
    "effective_annual_rate": "折算年化利率",
    "irr_annual_rate": "现金流内部收益率年化",
    "overdue_uplift_rate": "罚息上浮比例",
    "overdue_annual_rate": "罚息年化",
    "breach_penalty_ratio": "违约金比例",
    "prepayment_penalty_ratio": "提前还款违约金比例",
    "guarantee_deposit_ratio": "保证金比例",
}


def _parse_ratio_number(raw: str) -> float:
    """把"5"、"1.5"、"五"、"十二"、"三十五"统一解析为数值。"""
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("empty number")
    try:
        return float(cleaned.replace(",", ""))
    except ValueError:
        return _parse_chinese_number(cleaned)


def _parse_chinese_number(text: str) -> float:
    """支持 1-999 的中文数字，够用于利率表述。"""
    if not all(char in _CN_DIGITS or char in "十百千" for char in text):
        raise ValueError(f"无法解析的数字表述：{text}")

    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
            continue
        unit = {"十": 10, "百": 100, "千": 1000}[char]
        total += (current or 1) * unit
        current = 0
    return float(total + current)


def _percent_hit_to_ratio(raw: str) -> float:
    return _parse_ratio_number(raw) / 100.0


def _extract_rate_patterns(
    result: MetricSet, full_text: str, segments: list[dict]
) -> dict[str, float]:
    """抽取直述型利率，并返回各关键指标的最大值。"""
    maxima: dict[str, float] = {}

    for key, label, pattern, _unit in _RATE_PATTERNS:
        for match in re.finditer(pattern, full_text, re.IGNORECASE):
            ratio = _percent_hit_to_ratio(match.group(1))
            _add(
                result,
                key=key,
                label=label,
                kind="metric",
                full_text=full_text,
                segments=segments,
                start=match.start(),
                end=match.end(),
                value=ratio,
                display=f"{ratio * 100:.2f}%",
            )
            maxima[key] = max(maxima.get(key, 0.0), ratio)

            # 月/日利率必须同时给出年化折算，否则红线比对会漏判
            annual: float | None = None
            derivation = ""
            if key == "monthly_rate":
                annual = _annualize_monthly(ratio)
                derivation = f"月利率 {ratio * 100:.2f}% → 复利年化 {annual:.4%}"
            elif key == "daily_rate":
                annual = ratio * 365
                derivation = f"日利率 {ratio * 100:.4f}% → 按 365 天单利年化 {annual:.4%}"
            if annual is not None:
                _add(
                    result,
                    key="effective_annual_rate",
                    label=_KEY_TO_LABEL["effective_annual_rate"],
                    kind="metric",
                    full_text=full_text,
                    segments=segments,
                    start=match.start(),
                    end=match.end(),
                    value=annual,
                    display=f"{annual * 100:.2f}%",
                    derivation=derivation,
                )
                maxima["effective_annual_rate"] = max(
                    maxima.get("effective_annual_rate", 0.0), annual
                )

    return maxima


def _extract_implicit_daily_rates(
    result: MetricSet, full_text: str, segments: list[dict], maxima: dict[str, float]
) -> None:
    """抽取“日息万分之X”与“日利率X‰”写法。"""
    for pattern, divisor in ((_WFZH_PATTERN, 10_000.0), (_PERMILLE_DAILY, 1_000.0)):
        for match in re.finditer(pattern, full_text):
            daily = _parse_ratio_number(match.group(1)) / divisor
            annual = daily * 365
            _add(
                result,
                key="daily_rate",
                label=_KEY_TO_LABEL["daily_rate"],
                kind="metric",
                full_text=full_text,
                segments=segments,
                start=match.start(),
                end=match.end(),
                value=daily,
                display=f"{daily * 100:.4f}%",
                derivation=f"原文“{match.group(0).strip()}” → 日利率 {daily:.6f}",
            )
            _add(
                result,
                key="effective_annual_rate",
                label=_KEY_TO_LABEL["effective_annual_rate"],
                kind="metric",
                full_text=full_text,
                segments=segments,
                start=match.start(),
                end=match.end(),
                value=annual,
                display=f"{annual * 100:.2f}%",
                derivation=f"日利率 {daily:.6f} × 365 = {annual:.4%}",
            )
            maxima["daily_rate"] = max(maxima.get("daily_rate", 0.0), daily)
            maxima["effective_annual_rate"] = max(
                maxima.get("effective_annual_rate", 0.0), annual
            )


def _extract_monthly_fees(
    result: MetricSet, full_text: str, segments: list[dict], maxima: dict[str, float]
) -> None:
    """抽取“每月按本金 X% 收取服务费”类条款并年化。

    这类条款通常按初始本金计费，即使余额递减也照收，
    真实年化远高于表面数字，是消费信贷违规收费的高发区。
    """
    for pattern in (_MONTHLY_FEE_PATTERN, _FEE_MONTHLY_REVERSE):
        for match in re.finditer(pattern, full_text):
            ratio = _percent_hit_to_ratio(match.group(1))
            annual = _annualize_monthly(ratio)
            _add(
                result,
                key="monthly_fee_rate",
                label=_KEY_TO_LABEL["monthly_fee_rate"],
                kind="metric",
                full_text=full_text,
                segments=segments,
                start=match.start(),
                end=match.end(),
                value=ratio,
                display=f"{ratio * 100:.2f}%/月",
                derivation=f"月费率 {ratio:.4%} → 复利年化 {annual:.4%}",
            )
            _add(
                result,
                key="comprehensive_annual_rate",
                label=_KEY_TO_LABEL["comprehensive_annual_rate"],
                kind="metric",
                full_text=full_text,
                segments=segments,
                start=match.start(),
                end=match.end(),
                value=annual,
                display=f"{annual * 100:.2f}%",
                derivation=f"仅费用部分已年化至 {annual:.4%}，未计利息",
            )
            maxima["monthly_fee_rate"] = max(maxima.get("monthly_fee_rate", 0.0), ratio)
            maxima["comprehensive_annual_rate"] = max(
                maxima.get("comprehensive_annual_rate", 0.0), annual
            )


def _extract_cashflow_irr(
    result: MetricSet, full_text: str, segments: list[dict], maxima: dict[str, float]
) -> None:
    """用本金/期限/每期还款额构建现金流，求真实 IRR。"""
    principal_match = re.search(_PRINCIPAL_PATTERN, full_text)
    term_match = re.search(_TERM_PATTERN, full_text)
    installment_match = re.search(_INSTALLMENT_PATTERN, full_text)
    if not (principal_match and term_match and installment_match):
        return

    principal = _parse_number(principal_match.group(1))
    if principal_match.group(2) == "万元":
        principal *= 10_000

    term_value = _parse_number(term_match.group(1))
    unit = term_match.group(2)
    months = term_value if unit in ("个月", "月") else (
        term_value * 12 if unit == "年" else term_value / 30
    )
    months_int = round(months)
    installment = _parse_number(installment_match.group(1))

    if principal <= 0 or months_int <= 0 or installment <= 0:
        return

    cashflows = [principal] + [-installment] * months_int
    monthly_irr = _solve_irr(cashflows)
    if monthly_irr is None:
        result.warnings.append(
            "已读到本金/期限/每期还款额，但现金流无符号变化，IRR 无法求解"
        )
        return

    annual_irr = (1.0 + monthly_irr) ** 12 - 1.0
    span_start = min(m.start() for m in (principal_match, term_match, installment_match))
    span_end = max(m.end() for m in (principal_match, term_match, installment_match))
    _add(
        result,
        key="irr_annual_rate",
        label=_KEY_TO_LABEL["irr_annual_rate"],
        kind="metric",
        full_text=full_text,
        segments=segments,
        start=span_start,
        end=min(span_end, len(full_text)),
        value=annual_irr,
        display=f"{annual_irr * 100:.2f}%",
        derivation=(
            f"本金 {principal:,.2f} 元、{months_int} 期、每期 {installment:,.2f} 元 → "
            f"月度 IRR {monthly_irr:.6f} → 年化 {(1+monthly_irr)**12-1:.4%}"
        ),
    )
    maxima["irr_annual_rate"] = max(maxima.get("irr_annual_rate", 0.0), annual_irr)
    maxima["loan_term_months"] = months_int

    # 期限本身也要带原文定位，否则基于期限的红线无法高亮到条款
    _add(
        result,
        key="loan_term_months",
        label="借款期限",
        kind="metric",
        full_text=full_text,
        segments=segments,
        start=term_match.start(),
        end=term_match.end(),
        value=float(months_int),
        display=f"{months_int} 个月",
    )


def _extract_disbursement(
    result: MetricSet, full_text: str, segments: list[dict], maxima: dict[str, float]
) -> None:
    """比对约定本金与实际到账金额，量化“砍头息”。"""
    principal_match = re.search(_PRINCIPAL_PATTERN, full_text)
    disbursed_match = re.search(_DISBURSEMENT_PATTERN, full_text)
    if not (principal_match and disbursed_match):
        return

    principal = _parse_number(principal_match.group(1))
    if principal_match.group(2) == "万元":
        principal *= 10_000
    disbursed = _parse_number(disbursed_match.group(1))
    if disbursed_match.group(2) == "万元":
        disbursed *= 10_000
    if principal <= 0 or disbursed <= 0 or disbursed > principal:
        return

    shortfall = (principal - disbursed) / principal
    _add(
        result,
        key="disbursement_shortfall_ratio",
        label="本金预扣比例",
        kind="metric",
        full_text=full_text,
        segments=segments,
        start=disbursed_match.start(),
        end=disbursed_match.end(),
        value=shortfall,
        display=f"{shortfall * 100:.2f}%",
        derivation=(
            f"约定本金 {principal:,.2f} 元、实际到账 {disbursed:,.2f} 元 → "
            f"预扣比例 {(principal - disbursed) / principal:.4%}"
        ),
    )
    maxima["disbursement_shortfall_ratio"] = shortfall


def _extract_flags(result: MetricSet, full_text: str, segments: list[dict]) -> None:
    for key, label, pattern, guidance in _FLAG_PATTERNS:
        for match in re.finditer(pattern, full_text):
            _add(
                result,
                key=key,
                label=label,
                kind="flag",
                full_text=full_text,
                segments=segments,
                start=match.start(),
                end=match.end(),
                derivation=guidance,
            )


def extract_metrics(doc: ExtractedDocument, segments: list[dict]) -> MetricSet:
    """抽取全部数值指标与文本特征。结果写入 `Contract.extracted_metrics`。"""
    result = MetricSet()
    full_text = doc.text

    maxima = _extract_rate_patterns(result, full_text, segments)
    _extract_implicit_daily_rates(result, full_text, segments, maxima)
    _extract_monthly_fees(result, full_text, segments, maxima)
    _extract_cashflow_irr(result, full_text, segments, maxima)
    _extract_disbursement(result, full_text, segments, maxima)
    _extract_flags(result, full_text, segments)

    # 年化口径的“最不利值”：任一算法算出的最高年化成本
    annual_candidates = [
        value
        for key, value in maxima.items()
        if key
        in (
            "nominal_annual_rate",
            "comprehensive_annual_rate",
            "effective_annual_rate",
            "irr_annual_rate",
        )
    ]
    if annual_candidates:
        result.derived["max_annual_cost_rate"] = max(annual_candidates)
    else:
        # 故意不写入 0：取不到值时必须让规则引擎知道“指标缺失”，
        # 否则任何“利率 > 24%”类红线都会静默通过。
        _add(
            result,
            key="annual_cost_not_disclosed",
            label="未明示年化利率",
            kind="flag",
            full_text=full_text[:120],
            segments=segments,
            start=0,
            end=min(120, len(full_text)),
            derivation="全文未出现年利率、综合年化成本或可折算年化的日/月利率表述。",
        )
        result.warnings.append("未抽取到任何年化利率口径，监管要求以年化形式明示融资成本")

    # 只有"没有单一原文出处"的聚合口径才进 derived：
    # 否则 derived 会遮盖带 evidence 的同名指标，规则命中的溯源高亮就会丢失。
    evidenced = {item.key for item in result.evidences if item.value is not None}
    for key, value in maxima.items():
        if key not in evidenced:
            result.derived[key] = value
    return result


def metric_summary_for_prompt(metrics: MetricSet) -> str:
    """把抽取结果渲染成提示词片段，让模型基于确定性数值而不是自己算。"""
    lines: list[str] = []
    for evidence in metrics.evidences:
        if evidence.kind == "metric" and evidence.value is not None:
            note = f"（{evidence.derivation}）" if evidence.derivation else ""
            lines.append(f"- {evidence.label}：{evidence.display}{note} ← “{evidence.excerpt}”")
    if not lines:
        return "（未抽取到可量化指标）"
    return "\n".join(lines)
