/** 与后端 camelCase Schema 对应的前端类型。 */

export type ContractStatus =
  | "uploaded"
  | "parsing"
  | "parsed"
  | "reviewing"
  | "reviewed"
  | "failed";
export type ReviewStatus = "pending" | "running" | "succeeded" | "failed";
/** 复核执行形态：llm = 叫了复核模型；rule_check = 确定性规则复核；disabled = 本次未复核 */
export type JudgeMode = "llm" | "rule_check" | "disabled";
export type RiskLevel = "high" | "medium" | "low" | "none";
export type ContractKind =
  | "consumer_loan"
  | "small_loan"
  | "private_lending"
  | "guarantee"
  | "partnership"
  | "unknown";
export type FindingSource = "rule" | "llm" | "both";
export type CrossCheckStatus =
  | "consistent"
  | "rule_overrides"
  | "llm_only"
  | "rule_only";
export type HumanReviewStatus =
  | "not_required"
  | "pending"
  | "approved"
  | "rejected"
  | "adjusted";
export type HumanReviewReason =
  | "low_confidence"
  | "rule_llm_conflict"
  | "judge_disputed"
  | "high_risk_mandatory"
  | "unverified_citation"
  | "metric_missing"
  | "manual";

export type ClauseCategory =
  | "rate_and_fee"
  | "repayment"
  | "collateral_guarantee"
  | "breach_penalty"
  | "consumer_protection"
  | "personal_info"
  | "tied_product"
  | "dispute"
  | "subject_authority"
  | "disclosure"
  | "other";

export interface ContractSegment {
  index: number;
  title: string;
  text: string;
  charStart: number;
  charEnd: number;
  pageNo: number;
}

export interface MetricEvidence {
  key: string;
  label: string;
  kind: "metric" | "flag";
  value?: number | null;
  display: string;
  derivation: string;
  excerpt: string;
  charStart?: number | null;
  charEnd?: number | null;
  segmentIndex?: number | null;
  pageNo?: number | null;
}

export interface ContractBrief {
  id: string;
  title: string;
  contractKind: ContractKind;
  lenderName?: string | null;
  borrowerName?: string | null;
  fileName: string;
  fileSuffix: string;
  fileSizeBytes: number;
  pageCount: number;
  charCount: number;
  status: ContractStatus;
  createdAt: string;
  updatedAt: string;
  latestReviewId?: string | null;
  latestRiskScore?: number | null;
  latestNeedsHumanReview: boolean;
}

export interface ContractDetail extends ContractBrief {
  guaranteeProvider?: string | null;
  contentText: string;
  segments: ContractSegment[];
  metrics: MetricEvidence[];
  derivedMetrics: Record<string, number>;
  metricWarnings: string[];
  parseError?: string | null;
  ownerId?: string | null;
}

export interface ContractListResponse {
  items: ContractBrief[];
  meta: { total: number; page: number; pageSize: number };
}

export interface Citation {
  provisionId?: string | null;
  lawName: string;
  articleNo?: string | null;
  quote: string;
  docType?: string | null;
  isSample: boolean;
  verified: boolean;
  verifyReason?: string | null;
  effectiveOn?: string | null;
  repealedOn?: string | null;
  sourceUrl?: string | null;
  score?: number;
  matchKinds?: string[];
}

export interface Finding {
  id: string;
  riskLevel: RiskLevel;
  clauseCategory: ClauseCategory;
  title: string;
  excerpt: string;
  analysis: string;
  suggestion: string;
  rewrittenText?: string | null;
  source: FindingSource;
  crossCheckStatus: CrossCheckStatus;
  ruleCode?: string | null;
  metricSnapshot: Record<string, unknown>;
  confidence: number;
  llmConfidence: number;
  ruleConfidence: number;
  segmentIndex?: number | null;
  pageNo?: number | null;
  charStart?: number | null;
  charEnd?: number | null;
  citations: Citation[];
  hasUnverifiedCitation: boolean;
  /** 同类违规的监管罚单 id（仅在 RAG_USE_CASES 打开时非空） */
  similarCaseIds?: string[];
  /** 复核裁定快照：{judged, agree, citationOk, suggestedRiskLevel, confidence, reason, judgedBy} */
  judgeVerdict?: Record<string, unknown>;
  humanReviewStatus: HumanReviewStatus;
  humanReviewerId?: string | null;
  humanComment?: string | null;
  adjustedRiskLevel?: string | null;
  orderIndex: number;
}

export interface ReviewSummary {
  id: string;
  contractId: string;
  status: ReviewStatus;
  llmModel: string;
  embeddingModel: string;
  promptVersion: string;
  /** 复核通道；空串表示本次未执行复核，rule-check-v1 表示走了确定性规则复核 */
  judgeModel?: string;
  judgeCallCount?: number;
  judgeFailedCount?: number;
  /** 本次实际参与召回的通道（降级时必须可见） */
  retrievalChannels?: string[];
  riskScore: number;
  summary: string;
  highRiskCount: number;
  mediumRiskCount: number;
  lowRiskCount: number;
  ruleHitCount: number;
  llmFindingCount: number;
  conflictCount: number;
  retrievedChunkCount: number;
  llmCallCount: number;
  failedBatchCount: number;
  minConfidence: number;
  needsHumanReview: boolean;
  pendingHumanCount: number;
  elapsedSeconds: number;
  errorMessage?: string | null;
  createdAt: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  /** 存活心跳：排队/执行阶段区分“在跑”与“已死” */
  heartbeatAt?: string | null;
}

/** 轮询用的轻响应，避免每 3 秒拉一次完整报告。 */
export interface ReviewStatusResponse {
  id: string;
  contractId: string;
  status: ReviewStatus;
  elapsedSeconds: number;
  errorMessage?: string | null;
  pendingHumanCount: number;
  startedAt?: string | null;
  finishedAt?: string | null;
  heartbeatAt?: string | null;
}

export interface ReviewReport {
  review: ReviewSummary;
  findings: Finding[];
  referenceProvisions: Citation[];
  /** 同类违规的监管罚单快照（类比参考，不是法条依据） */
  referenceCases?: ReferenceCase[];
  metricWarnings: string[];
}

/** 监管罚单快照，字段与后端 `RetrievedCase.to_snapshot()` 对齐。 */
export interface ReferenceCase {
  caseId: string;
  orgName: string;
  summary: string;
  penaltyContent?: string | null;
  caseNo?: string | null;
  penaltyBody?: string | null;
  decidedOn?: string | null;
  violationTypes?: string[];
  penaltyAmount?: number | null;
  sourceUrl?: string | null;
  isSample?: boolean;
  score?: number;
}

export interface ReviewRunRequest {
  perspective?: "neutral" | "consumer" | "institution";
  focusCategories?: ClauseCategory[];
  customInstructions?: string | null;
  ruleOnly?: boolean;
  topK?: number | null;
}

export interface HumanReviewTask {
  id: string;
  findingId: string;
  reviewId: string;
  contractId: string;
  reason: HumanReviewReason;
  status: HumanReviewStatus;
  assignee?: string | null;
  comment?: string | null;
  expertVerdict?: string | null;
  expertRiskLevel?: string | null;
  createdAt: string;
  resolvedAt?: string | null;
  exportedForSft: boolean;
  findingTitle?: string | null;
  findingExcerpt?: string | null;
  findingConfidence?: number | null;
  findingSource?: FindingSource | null;
  riskLevel?: RiskLevel | null;
}

export interface RedLineRule {
  id: string;
  code: string;
  name: string;
  category: ClauseCategory;
  severity: RiskLevel;
  appliesTo: string[];
  metricKey?: string | null;
  operator?: string | null;
  thresholdSource: string;
  thresholdValue?: number | null;
  unit: string;
  tolerance: number;
  secondaryThreshold?: number | null;
  keywordPatterns: string[];
  hardBlock: boolean;
  llmFallback: boolean;
  hitConfidence: number;
  basisRefs: Record<string, unknown>[];
  guidanceText: string;
  suggestionText: string;
  priority: number;
  isBuiltin: boolean;
  isActive: boolean;
}

export interface RuleHit {
  ruleCode: string;
  ruleName: string;
  severity: RiskLevel;
  category: ClauseCategory;
  hardBlock: boolean;
  title: string;
  analysis: string;
  suggestion: string;
  confidence: number;
  needsLlmReview: boolean;
  metricKey?: string | null;
  metricValue?: number | null;
  threshold?: number | null;
  thresholdDesc: string;
  derivation: string;
  excerpt: string;
  pageNo?: number | null;
  segmentIndex?: number | null;
  basisRefs: Record<string, unknown>[];
}

export interface RuleDryRunResponse {
  hits: RuleHit[];
  inconclusive: { code: string; name: string }[];
  weakSignals: string[];
  checkedRuleCount: number;
  metrics: {
    evidences: MetricEvidence[];
    derived: Record<string, number>;
    warnings: string[];
  };
}

export interface ThresholdSnapshot {
  lprOneYearRate: number;
  lprMultiple: number;
  judicialProtectionCeiling: number;
  regulatoryCostCeiling: number;
  historicalHardCeiling: number;
  confidenceRoutingThreshold: number;
  /** 可编辑口径清单（含来源、生效日与维护人） */
  items?: ThresholdItem[];
}

export interface ThresholdItem {
  key: string;
  label: string;
  unit: string;
  value: number;
  min: number;
  max: number;
  description: string;
  /** db = 库内生效值；settings = 仍取环境变量默认 */
  source: string;
  effectiveOn?: string | null;
  updatedBy?: string | null;
}

export interface LegalProvision {
  id: string;
  lawName: string;
  articleNo: string;
  chapter?: string | null;
  title?: string | null;
  content: string;
  docType: string;
  issuingBody?: string | null;
  effectiveOn?: string | null;
  repealedOn?: string | null;
  versionLabel?: string | null;
  sourceUrl?: string | null;
  isSample: boolean;
  isActive: boolean;
  hasEmbedding: boolean;
}

export interface HealthResponse {
  status: "ok" | "degraded";
  app: string;
  environment: string;
  version: string;
  database: string;
  llmProvider: string;
  llmModel: string;
  /** 主审通道（环节3/4）的思考深度；default = 沿用供应商最深档 */
  llmReasoningEffort: string;
  /** 复核通道（环节7）的思考深度；空串表示本次未启用独立复核 */
  judgeReasoningEffort: string;
  judgeModel: string;
  judgeMode: JudgeMode;
  /** false = 复核与主审同源，不构成独立信号（回音壁） */
  judgeIndependent: boolean;
  embeddingProvider: string;
  embeddingIsMock: boolean;
  /** 稠密向量后端（qdrant）；权威文本仍以 Postgres 为单一数据源 */
  denseStore?: string;
  sparseEnabled?: boolean;
  rerankEnabled?: boolean;
  casesEnabled?: boolean;
  queueEnabled: boolean;
  queueState: string;
  /** 队列积压；null = 无法确认 */
  queueDepth?: number | null;
  /** 是否有活着的 worker 在消费（ARQ worker 心跳键）；null = 无法确认 */
  queueWorkerAlive?: boolean | null;
  zombieTimeoutSeconds: number;
  knowledgeProvisionCount: number;
  redLineRuleCount: number;
  /** “能跑但不该上线”的配置口径清单 */
  configWarnings?: string[];
}
