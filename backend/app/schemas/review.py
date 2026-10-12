"""审查相关 Schema。

`*Draft` 系列同时充当 LLM 的结构化输出契约：模型必须按此形状返回，
解析失败即视为该批次失败，不做"尽力而为"的宽松兼容——
把半截 JSON 塞进报告比审查中断更糟。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.enums import (
    ClauseCategory,
    CrossCheckStatus,
    FindingSource,
    HumanReviewReason,
    HumanReviewStatus,
    ReviewStatus,
    RiskLevel,
)
from app.schemas.common import CamelModel, PageMeta

# ---------- 入站 ----------


class ReviewRunRequest(CamelModel):
    """发起审查的可选项。"""

    perspective: str = "neutral"  # neutral | consumer | institution
    focus_categories: list[ClauseCategory] = []
    custom_instructions: str | None = None
    #: 是否跳过 LLM 通道，只跑规则引擎（离线/快速预检场景）
    rule_only: bool = False
    #: 检索条数覆盖全局配置
    top_k: int | None = None


class HumanReviewResolveRequest(CamelModel):
    """法务复核结论。"""

    verdict: str  # confirmed | false_positive | need_adjustment
    reviewer_id: str = "legal"
    comment: str = ""
    adjusted_risk_level: RiskLevel | None = None


class CitationDraft(CamelModel):
    """模型给出的引用，落库前必须经 `citation.resolve_citations` 核验。"""

    provision_id: str | None = None
    law_name: str = ""
    article_no: str = ""
    quote: str = ""
    doc_type: str = ""


class ReviewFindingDraft(CamelModel):
    """LLM 返回的单条结论。"""

    risk_level: RiskLevel = RiskLevel.LOW
    clause_category: ClauseCategory = ClauseCategory.OTHER
    title: str
    excerpt: str = ""
    analysis: str = ""
    suggestion: str = ""
    rewritten_text: str | None = None
    confidence: float = 0.6
    segment_index: int | None = None
    citations: list[CitationDraft] = []
    rule_code: str | None = None
    #: 模型回指的参考判例 id；不在本轮检索清单内的 id 在管道里直接丢弃
    similar_case_ids: list[str] = []


class ReviewBatchDraft(CamelModel):
    """一轮模型调用的完整输出。"""

    batch_summary: str = ""
    findings: list[ReviewFindingDraft] = []


class JudgeVerdictDraft(CamelModel):
    """复核（judge）模型对某一条结论的裁定。

    `index` 是送审列表里的序号，不是数据库主键：用序号回连才能把
    “哪条结论被质疑”对上号，避免模型自己编 id。
    """

    index: int
    agree: bool = True
    citation_ok: bool = True
    suggested_risk_level: RiskLevel | None = None
    confidence: float = 0.5
    reason: str = ""


class JudgeBatchDraft(CamelModel):
    """一轮复核调用的输出。"""

    verdicts: list[JudgeVerdictDraft] = []


# ---------- 出站 ----------


class CitationOut(CamelModel):
    provision_id: str | None = None
    law_name: str = ""
    article_no: str | None = None
    quote: str = ""
    doc_type: str = ""
    is_sample: bool = True
    verified: bool = False
    verify_reason: str | None = None
    effective_on: str | None = None
    repealed_on: str | None = None
    source_url: str | None = None


class FindingOut(CamelModel):
    id: uuid.UUID
    risk_level: RiskLevel
    clause_category: ClauseCategory
    title: str
    excerpt: str
    analysis: str
    suggestion: str
    rewritten_text: str | None = None

    source: FindingSource
    cross_check_status: CrossCheckStatus
    rule_code: str | None = None
    metric_snapshot: dict = {}

    confidence: float
    llm_confidence: float
    rule_confidence: float

    segment_index: int | None = None
    page_no: int | None = None
    char_start: int | None = None
    char_end: int | None = None

    citations: list[dict] = []
    has_unverified_citation: bool = False
    #: 同类违规的监管罚单 id（仅在 RAG_USE_CASES 打开时非空）
    similar_case_ids: list[str] = []

    #: 复核裁定（{judged, agree, citationOk, suggestedRiskLevel, confidence, reason, judgedBy}）
    judge_verdict: dict = {}

    human_review_status: HumanReviewStatus
    human_reviewer_id: str | None = None
    human_comment: str | None = None
    adjusted_risk_level: str | None = None
    order_index: int


class ReviewSummary(CamelModel):
    id: uuid.UUID
    contract_id: uuid.UUID
    status: ReviewStatus
    llm_model: str
    embedding_model: str
    prompt_version: str
    #: 复核通道；空串表示本次未执行复核，`rule-check-v1` 表示走了确定性规则复核
    judge_model: str = ""
    judge_call_count: int = 0
    judge_failed_count: int = 0
    #: 实际参与召回的通道，降级时前端需要据此提示
    retrieval_channels: list[str] = []
    risk_score: int
    summary: str
    high_risk_count: int
    medium_risk_count: int
    low_risk_count: int
    rule_hit_count: int
    llm_finding_count: int
    conflict_count: int
    retrieved_chunk_count: int
    llm_call_count: int
    failed_batch_count: int
    min_confidence: float
    needs_human_review: bool
    pending_human_count: int
    elapsed_seconds: float
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: 存活心跳：巡检靠它区分“在跑”与“僵尸”，没有它就只能拿 updated_at 猜
    heartbeat_at: datetime | None = None


class ReviewStatusOut(CamelModel):
    """轮询用的轻响应。

    报告页以前每 3 秒拉一次完整报告（结论 + 引用 + 条文快照），
    排队阶段这些数据根本不存在，轮询自身就成了新的负载源。
    """

    id: uuid.UUID
    contract_id: uuid.UUID
    status: ReviewStatus
    elapsed_seconds: float = 0.0
    error_message: str | None = None
    pending_human_count: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None


class ReviewReport(CamelModel):
    review: ReviewSummary
    findings: list[FindingOut]
    #: 检索到但未被任何结论引用的条文，前端"参考依据"区展示
    reference_provisions: list[CitationOut] = []
    #: 同类违规的监管罚单快照（仅在 RAG_USE_CASES 打开时非空），只作类比参考
    reference_cases: list[dict] = []
    metric_warnings: list[str] = []


class ReviewListResponse(CamelModel):
    items: list[ReviewSummary]
    meta: PageMeta


class HumanReviewTaskOut(CamelModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    review_id: uuid.UUID
    contract_id: uuid.UUID
    reason: HumanReviewReason
    status: HumanReviewStatus
    assignee: str | None = None
    comment: str | None = None
    expert_verdict: str | None = None
    expert_risk_level: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    exported_for_sft: bool = False
    #: 便于列表页直接展示，不额外拉一次详情
    finding_title: str | None = None
    finding_excerpt: str | None = None
    finding_confidence: float | None = None
    finding_source: FindingSource | None = None
    risk_level: RiskLevel | None = None


class HumanReviewQueueResponse(CamelModel):
    items: list[HumanReviewTaskOut]
    meta: PageMeta
