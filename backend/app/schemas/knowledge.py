"""知识库 Schema。"""

from __future__ import annotations

import uuid
from datetime import date

from app.models.enums import KnowledgeDocType
from app.schemas.common import CamelModel, PageMeta


class ProvisionIn(CamelModel):
    """导入一条权威条文原文。

    `content` 必须以官方发布文本为准；系统不会替你补全条文。
    """

    law_name: str
    article_no: str
    content: str
    doc_type: KnowledgeDocType = KnowledgeDocType.DEPARTMENT_RULE
    chapter: str | None = None
    title: str | None = None
    issuing_body: str | None = None
    effective_on: date | None = None
    repealed_on: date | None = None
    version_label: str | None = None
    source_url: str | None = None


class ProvisionOut(CamelModel):
    id: uuid.UUID
    law_name: str
    article_no: str
    chapter: str | None = None
    title: str | None = None
    content: str
    doc_type: KnowledgeDocType
    issuing_body: str | None = None
    effective_on: date | None = None
    repealed_on: date | None = None
    version_label: str | None = None
    source_url: str | None = None
    is_sample: bool
    is_active: bool
    has_embedding: bool = False


class ProvisionSearchResponse(CamelModel):
    query: str
    items: list[ProvisionOut]
    match_kinds: list[str] = []
    embedding_provider: str = ""
    #: true 时说明相似度不可信，前端必须提示
    embedding_is_mock: bool = False
    meta: PageMeta


class EnforcementCaseIn(CamelModel):
    org_name: str
    summary: str
    case_no: str | None = None
    penalty_body: str | None = None
    decided_on: date | None = None
    penalty_content: str = ""
    penalty_amount: float | None = None
    violation_types: list[str] = []
    basis_refs: list[dict] = []
    source_url: str | None = None


class EnforcementCaseOut(CamelModel):
    id: uuid.UUID
    case_no: str | None = None
    org_name: str
    penalty_body: str | None = None
    decided_on: date | None = None
    summary: str
    penalty_content: str
    penalty_amount: float | None = None
    violation_types: list[str] = []
    basis_refs: list[dict] = []
    source_url: str | None = None
    is_sample: bool
    has_embedding: bool = False


class CaseSearchResponse(CamelModel):
    query: str
    items: list[EnforcementCaseOut]
    embedding_is_mock: bool = False


class ReindexResult(CamelModel):
    """一次向量/BM25 重建的结果，降级情形必须明说而不是默返 0。"""

    embeddings_rebuilt: int = 0
    sparse_synced: int = 0
    sparse_enabled: bool = False
    sparse_available: bool = False
    message: str = ""


class IngestRunRequest(CamelModel):
    """触发监管公开信息采集。

    默认关闭且必须显式传入 `confirmed_compliance_ok=true`，
    因为对外部站点的抓取责任在使用方。
    """

    source: str = "nfra_penalty"
    limit: int | None = None
    confirmed_compliance_ok: bool = False


class IngestRunOut(CamelModel):
    id: uuid.UUID
    source: str
    doc_type: str
    item_count: int
    ok_count: int
    skipped_count: int
    status: str
    message: str | None = None
    created_at: object


class KnowledgeStats(CamelModel):
    provision_total: int
    provision_imported: int
    provision_placeholder: int
    case_total: int
    rule_total: int
    rule_active: int
    embedding_provider: str
    embedding_is_mock: bool
