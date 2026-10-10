"""合同相关 Schema。"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.models.enums import ContractKind, ContractStatus
from app.schemas.common import CamelModel, PageMeta


class ContractSegment(CamelModel):
    """按条款切分后的片段，偏移与页码用于 PDF.js 高亮。"""

    index: int
    title: str
    text: str
    char_start: int
    char_end: int
    page_no: int


class MetricEvidence(CamelModel):
    """一次指标/特征命中，含计算过程与原文定位。"""

    key: str
    label: str
    kind: str
    value: float | None = None
    display: str = ""
    derivation: str = ""
    excerpt: str = ""
    char_start: int | None = None
    char_end: int | None = None
    segment_index: int | None = None
    page_no: int | None = None


class ContractBrief(CamelModel):
    id: uuid.UUID
    title: str
    contract_kind: ContractKind
    lender_name: str | None = None
    borrower_name: str | None = None
    file_name: str
    file_suffix: str
    file_size_bytes: int
    page_count: int
    char_count: int
    status: ContractStatus
    created_at: datetime
    updated_at: datetime
    latest_review_id: uuid.UUID | None = None
    latest_risk_score: int | None = None
    latest_needs_human_review: bool = False


class ContractDetail(ContractBrief):
    guarantee_provider: str | None = None
    content_text: str
    segments: list[ContractSegment] = []
    metrics: list[MetricEvidence] = []
    derived_metrics: dict[str, float] = {}
    metric_warnings: list[str] = []
    parse_error: str | None = None
    owner_id: str | None = None


class ContractListResponse(CamelModel):
    items: list[ContractBrief]
    meta: PageMeta


class ContractUpdateRequest(CamelModel):
    title: str | None = None
    contract_kind: ContractKind | None = None
    lender_name: str | None = None
    borrower_name: str | None = None
    guarantee_provider: str | None = None
