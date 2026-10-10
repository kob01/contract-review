/** 与后端 camelCase Schema 对应的前端类型。 */

export type ContractStatus = 'uploaded' | 'parsing' | 'parsed' | 'reviewing' | 'reviewed' | 'failed'
export type ReviewStatus = 'pending' | 'running' | 'succeeded' | 'failed'
export type RiskLevel = 'high' | 'medium' | 'low' | 'none'
export type ContractKind =
  | 'consumer_loan'
  | 'small_loan'
  | 'private_lending'
  | 'guarantee'
  | 'partnership'
  | 'unknown'
export type FindingSource = 'rule' | 'llm' | 'both'
export type CrossCheckStatus = 'consistent' | 'rule_overrides' | 'llm_only' | 'rule_only'
export type HumanReviewStatus = 'not_required' | 'pending' | 'approved' | 'rejected' | 'adjusted'
export type HumanReviewReason =
  | 'low_confidence'
  | 'rule_llm_conflict'
  | 'high_risk_mandatory'
  | 'unverified_citation'
  | 'metric_missing'
  | 'manual'

export type ClauseCategory =
  | 'rate_and_fee'
  | 'repayment'
  | 'collateral_guarantee'
  | 'breach_penalty'
  | 'consumer_protection'
  | 'personal_info'
  | 'tied_product'
  | 'dispute'
  | 'subject_authority'
  | 'disclosure'
  | 'other'

export interface ContractSegment {
  index: number
  title: string
  text: string
  charStart: number
  charEnd: number
  pageNo: number
}

export interface MetricEvidence {
  key: string
  label: string
  kind: 'metric' | 'flag'
  value?: number | null
  display: string
  derivation: string
  excerpt: string
  charStart?: number | null
  charEnd?: number | null
  segmentIndex?: number | null
  pageNo?: number | null
}

export interface ContractBrief {
  id: string
  title: string
  contractKind: ContractKind
  lenderName?: string | null
  borrowerName?: string | null
  fileName: string
  fileSuffix: string
  fileSizeBytes: number
  pageCount: number
  charCount: number
  status: ContractStatus
  createdAt: string
  updatedAt: string
  latestReviewId?: string | null
  latestRiskScore?: number | null
  latestNeedsHumanReview: boolean
}

export interface ContractDetail extends ContractBrief {
  guaranteeProvider?: string | null
  contentText: string
  segments: ContractSegment[]
  metrics: MetricEvidence[]
  derivedMetrics: Record<string, number>
  metricWarnings: string[]
  parseError?: string | null
  ownerId?: string | null
}

export interface ContractListResponse {
  items: ContractBrief[]
  meta: { total: number; page: number; pageSize: number }
}

export interface Citation {
  provisionId?: string | null
  lawName: string
  articleNo?: string | null
  quote: string
  docType?: string | null
  isSample: boolean
  verified: boolean
  verifyReason?: string | null
  effectiveOn?: string | null
  repealedOn?: string | null
  sourceUrl?: string | null
  score?: number
  matchKinds?: string[]
}

export interface Finding {
  id: string
  riskLevel: RiskLevel
  clauseCategory: ClauseCategory
  title: string
  excerpt: string
  analysis: string
  suggestion: string
  rewrittenText?: string | null
  source: FindingSource
  crossCheckStatus: CrossCheckStatus
  ruleCode?: string | null
  metricSnapshot: Record<string, unknown>
  confidence: number
  llmConfidence: number
  ruleConfidence: number
  segmentIndex?: number | null
  pageNo?: number | null
  charStart?: number | null
  charEnd?: number | null
  citations: Citation[]
  hasUnverifiedCitation: boolean
  humanReviewStatus: HumanReviewStatus
  humanReviewerId?: string | null
  humanComment?: string | null
  adjustedRiskLevel?: string | null
  orderIndex: number
}

export interface ReviewSummary {
  id: string
  contractId: string
  status: ReviewStatus
  llmModel: string
  embeddingModel: string
  promptVersion: string
  riskScore: number
  summary: string
  highRiskCount: number
  mediumRiskCount: number
  lowRiskCount: number
  ruleHitCount: number
  llmFindingCount: number
  conflictCount: number
  retrievedChunkCount: number
  llmCallCount: number
  failedBatchCount: number
  minConfidence: number
  needsHumanReview: boolean
  pendingHumanCount: number
  elapsedSeconds: number
  errorMessage?: string | null
  createdAt: string
  startedAt?: string | null
  finishedAt?: string | null
}

export interface ReviewReport {
  review: ReviewSummary
  findings: Finding[]
  referenceProvisions: Citation[]
  metricWarnings: string[]
}

export interface ReviewRunRequest {
  perspective?: 'neutral' | 'consumer' | 'institution'
  focusCategories?: ClauseCategory[]
  customInstructions?: string | null
  ruleOnly?: boolean
  topK?: number | null
}

export interface HumanReviewTask {
  id: string
  findingId: string
  reviewId: string
  contractId: string
  reason: HumanReviewReason
  status: HumanReviewStatus
  assignee?: string | null
  comment?: string | null
  expertVerdict?: string | null
  expertRiskLevel?: string | null
  createdAt: string
  resolvedAt?: string | null
  exportedForSft: boolean
  findingTitle?: string | null
  findingExcerpt?: string | null
  findingConfidence?: number | null
  findingSource?: FindingSource | null
  riskLevel?: RiskLevel | null
}

export interface RedLineRule {
  id: string
  code: string
  name: string
  category: ClauseCategory
  severity: RiskLevel
  appliesTo: string[]
  metricKey?: string | null
  operator?: string | null
  thresholdSource: string
  thresholdValue?: number | null
  unit: string
  tolerance: number
  secondaryThreshold?: number | null
  keywordPatterns: string[]
  hardBlock: boolean
  llmFallback: boolean
  hitConfidence: number
  basisRefs: Record<string, unknown>[]
  guidanceText: string
  suggestionText: string
  priority: number
  isBuiltin: boolean
  isActive: boolean
}

export interface RuleHit {
  ruleCode: string
  ruleName: string
  severity: RiskLevel
  category: ClauseCategory
  hardBlock: boolean
  title: string
  analysis: string
  suggestion: string
  confidence: number
  needsLlmReview: boolean
  metricKey?: string | null
  metricValue?: number | null
  threshold?: number | null
  thresholdDesc: string
  derivation: string
  excerpt: string
  pageNo?: number | null
  segmentIndex?: number | null
  basisRefs: Record<string, unknown>[]
}

export interface RuleDryRunResponse {
  hits: RuleHit[]
  inconclusive: { code: string; name: string }[]
  weakSignals: string[]
  checkedRuleCount: number
  metrics: { evidences: MetricEvidence[]; derived: Record<string, number>; warnings: string[] }
}

export interface ThresholdSnapshot {
  lprOneYearRate: number
  lprMultiple: number
  judicialProtectionCeiling: number
  regulatoryCostCeiling: number
  historicalHardCeiling: number
  confidenceRoutingThreshold: number
}

export interface LegalProvision {
  id: string
  lawName: string
  articleNo: string
  chapter?: string | null
  title?: string | null
  content: string
  docType: string
  issuingBody?: string | null
  effectiveOn?: string | null
  repealedOn?: string | null
  versionLabel?: string | null
  sourceUrl?: string | null
  isSample: boolean
  isActive: boolean
  hasEmbedding: boolean
}

export interface HealthResponse {
  status: 'ok' | 'degraded'
  app: string
  environment: string
  version: string
  database: string
  llmProvider: string
  llmModel: string
  embeddingProvider: string
  embeddingIsMock: boolean
  knowledgeProvisionCount: number
  redLineRuleCount: number
}
