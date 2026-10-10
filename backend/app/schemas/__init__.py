"""Pydantic 请求/响应模型。"""

from app.schemas.common import CamelModel, HealthResponse, PageMeta
from app.schemas.contract import (
    ContractBrief,
    ContractDetail,
    ContractListResponse,
    ContractSegment,
    ContractUpdateRequest,
    MetricEvidence,
)
from app.schemas.knowledge import (
    CaseSearchResponse,
    EnforcementCaseIn,
    EnforcementCaseOut,
    IngestRunOut,
    IngestRunRequest,
    KnowledgeStats,
    ProvisionIn,
    ProvisionOut,
    ProvisionSearchResponse,
)
from app.schemas.redline import (
    RedLineRuleOut,
    RedLineRuleUpsertRequest,
    RuleDryRunRequest,
    RuleDryRunResponse,
    RuleHitOut,
    ThresholdSnapshot,
)
from app.schemas.review import (
    CitationDraft,
    CitationOut,
    FindingOut,
    HumanReviewQueueResponse,
    HumanReviewResolveRequest,
    HumanReviewTaskOut,
    ReviewBatchDraft,
    ReviewFindingDraft,
    ReviewListResponse,
    ReviewReport,
    ReviewRunRequest,
    ReviewSummary,
)

__all__ = [
    "CamelModel",
    # 知识库
    "CaseSearchResponse",
    # 审查
    "CitationDraft",
    "CitationOut",
    # 合同
    "ContractBrief",
    "ContractDetail",
    "ContractListResponse",
    "ContractSegment",
    "ContractUpdateRequest",
    "EnforcementCaseIn",
    "EnforcementCaseOut",
    "FindingOut",
    "HealthResponse",
    "HumanReviewQueueResponse",
    "HumanReviewResolveRequest",
    "HumanReviewTaskOut",
    "IngestRunOut",
    "IngestRunRequest",
    "KnowledgeStats",
    "MetricEvidence",
    "PageMeta",
    "ProvisionIn",
    "ProvisionOut",
    "ProvisionSearchResponse",
    # 红线规则
    "RedLineRuleOut",
    "RedLineRuleUpsertRequest",
    "ReviewBatchDraft",
    "ReviewFindingDraft",
    "ReviewListResponse",
    "ReviewReport",
    "ReviewRunRequest",
    "ReviewSummary",
    "RuleDryRunRequest",
    "RuleDryRunResponse",
    "RuleHitOut",
    "ThresholdSnapshot",
]
