import type {
  ClauseCategory,
  ContractKind,
  ContractStatus,
  CrossCheckStatus,
  FindingSource,
  HumanReviewReason,
  HumanReviewStatus,
  RiskLevel,
} from "@/types";

export const riskLabels: Record<RiskLevel, string> = {
  high: "高风险",
  medium: "中风险",
  low: "低风险",
  none: "无风险",
};

export const riskTone: Record<RiskLevel, string> = {
  high: "bg-red-50 text-red-700 ring-red-600/20",
  medium: "bg-amber-50 text-amber-700 ring-amber-600/20",
  low: "bg-blue-50 text-blue-700 ring-blue-600/20",
  none: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
};

/** PDF 高亮色与风险等级一致，保证左右两栏视觉对应。 */
export const riskHighlight: Record<RiskLevel, string> = {
  high: "rgba(220, 38, 38, 0.28)",
  medium: "rgba(245, 158, 11, 0.30)",
  low: "rgba(37, 99, 235, 0.24)",
  none: "rgba(22, 163, 74, 0.22)",
};

export const sourceLabels: Record<FindingSource, string> = {
  rule: "规则引擎",
  llm: "模型 + RAG",
  both: "双通道一致",
};

export const sourceTone: Record<FindingSource, string> = {
  rule: "bg-slate-900 text-white",
  llm: "bg-violet-100 text-violet-700",
  both: "bg-emerald-100 text-emerald-700",
};

export const crossCheckLabels: Record<CrossCheckStatus, string> = {
  consistent: "双通道一致",
  rule_overrides: "冲突，以规则为准",
  llm_only: "仅模型判定",
  rule_only: "仅规则命中",
};

export const categoryLabels: Record<ClauseCategory, string> = {
  rate_and_fee: "利率与费用",
  repayment: "还款与期限",
  collateral_guarantee: "担保与保证金",
  breach_penalty: "违约与催收",
  consumer_protection: "消费者保护",
  personal_info: "个人信息",
  tied_product: "搭售与增值",
  dispute: "争议解决",
  subject_authority: "主体与资质",
  disclosure: "信息披露",
  other: "其他",
};

export const contractStatusLabels: Record<ContractStatus, string> = {
  uploaded: "待解析",
  parsing: "解析中",
  parsed: "已解析",
  reviewing: "审查中",
  reviewed: "已审查",
  failed: "失败",
};

export const contractKindLabels: Record<ContractKind, string> = {
  consumer_loan: "消费信贷",
  small_loan: "小额贷款",
  private_lending: "民间借贷",
  guarantee: "担保/抵押",
  partnership: "助贷/合作",
  unknown: "未识别",
};

export const humanReviewLabels: Record<HumanReviewStatus, string> = {
  not_required: "无需复核",
  pending: "待复核",
  approved: "已确认",
  rejected: "已驳回（误报）",
  adjusted: "已调级",
};

export const humanReviewTone: Record<HumanReviewStatus, string> = {
  not_required: "bg-slate-100 text-slate-600",
  pending: "bg-orange-100 text-orange-700",
  approved: "bg-emerald-100 text-emerald-700",
  rejected: "bg-slate-200 text-slate-700",
  adjusted: "bg-blue-100 text-blue-700",
};

export const reasonLabels: Record<HumanReviewReason, string> = {
  low_confidence: "置信度未达标",
  rule_llm_conflict: "双通道结论冲突",
  judge_disputed: "复核模型不认可或把握不足",
  high_risk_mandatory: "高风险强制确认",
  unverified_citation: "引用依据未核验",
  metric_missing: "关键数值无法抽取",
  manual: "人工挂起",
};

/** 比率显示：0.031 -> 3.10%。 */
export const formatPercent = (value?: number | null, digits = 2): string =>
  value === null || value === undefined
    ? "—"
    : `${(value * 100).toFixed(digits)}%`;

export const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

export const formatTime = (iso: string): string =>
  new Date(iso).toLocaleString("zh-CN");
