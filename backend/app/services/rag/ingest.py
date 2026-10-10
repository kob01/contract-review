"""法条与罚单的入库、占位登记与向量重建。

关于内置数据的一条硬约束：
本仓库**不内置任何条文原文**。内置的只是"法规名 + 条号"的占位登记
（`is_sample=True`），原文需由 `POST /api/v1/knowledge/provisions` 从官方渠道导入。
原因是把记忆中的条文当成权威文本喂给模型，正是法律 AI 最危险的幻觉来源：
检索到的引用本身是假的，后面的推理再严密也没有意义。

占位记录仍然会被检索命中，但审查管道会把它标成"原文未核验"并强制转人工，
让"缺数据"表现为待办，而不是表现为结论。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import KnowledgeDocType
from app.models.knowledge import EnforcementCase, LegalProvision
from app.services.rag.embeddings import EmbeddingClient
from app.services.rules.builtin_redlines import BUILTIN_RED_LINES

logger = get_logger(__name__)

PLACEHOLDER_PREFIX = "【原文待导入】"

#: 内置登记的法规名称与效力层级，不含条文原文
_CORE_REGISTRY: tuple[tuple[str, str], ...] = (
    ("中华人民共和国民法典", KnowledgeDocType.LAW.value),
    ("中华人民共和国消费者权益保护法", KnowledgeDocType.LAW.value),
    ("中华人民共和国个人信息保护法", KnowledgeDocType.LAW.value),
    ("中华人民共和国民事诉讼法", KnowledgeDocType.LAW.value),
    ("最高人民法院关于审理民间借贷案件适用法律若干问题的规定", KnowledgeDocType.JUDICIAL_INTERPRETATION.value),
    ("银行保险机构消费者权益保护管理办法", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("消费金融公司管理办法", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("个人贷款管理办法", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("商业银行互联网贷款管理暂行办法", KnowledgeDocType.DEPARTMENT_RULE.value),
    ("融资担保公司监督管理条例", KnowledgeDocType.ADMIN_REGULATION.value),
    ("中国人民银行关于明示贷款年化利率的公告", KnowledgeDocType.REGULATORY_GUIDE.value),
)


@dataclass(slots=True)
class ProvisionInput:
    law_name: str
    article_no: str
    content: str
    doc_type: str = KnowledgeDocType.DEPARTMENT_RULE.value
    chapter: str | None = None
    title: str | None = None
    issuing_body: str | None = None
    effective_on: object | None = None
    repealed_on: object | None = None
    version_label: str | None = None
    source_url: str | None = None


def referenced_provisions() -> list[tuple[str, str | None, str]]:
    """收集红线规则引用到的（法规名, 条号, 规则编码）。"""
    seen: dict[tuple[str, str | None], set[str]] = {}
    for rule in BUILTIN_RED_LINES:
        for ref in rule.basis_refs:
            law_name = ref.get("law_name")
            if not law_name:
                continue
            article_no = ref.get("article_no")
            seen.setdefault((law_name, article_no), set()).add(rule.code)
    return [
        (law_name, article_no, ",".join(sorted(codes)))
        for (law_name, article_no), codes in seen.items()
    ]


def provision_dedup_key(law_name: str, article_no: str, content: str) -> str:
    digest = hashlib.sha256(f"{law_name}#{article_no}#{content}".encode()).hexdigest()
    return digest[:32]


async def upsert_provision(
    session: AsyncSession,
    payload: ProvisionInput,
    *,
    embedder: EmbeddingClient | None = None,
) -> tuple[LegalProvision, bool]:
    """按（法规名, 条号）写入或更新条文。

    只有 `content` 不是占位前缀开头时才视为权威原文（`is_sample=False`）。
    返回 (记录, 是否新建)。
    """
    result = await session.execute(
        select(LegalProvision).where(
            LegalProvision.law_name == payload.law_name,
            LegalProvision.article_no == payload.article_no,
        )
    )
    rows = list(result.scalars().all())
    row = rows[0] if rows else None
    is_placeholder = payload.content.startswith(PLACEHOLDER_PREFIX)
    embedding = None
    if embedder is not None and not is_placeholder:
        embedding = (await embedder.embed_texts([_embedding_text(payload)]))[0]

    if row is None:
        row = LegalProvision(
            law_name=payload.law_name,
            article_no=payload.article_no,
            content=payload.content,
            doc_type=payload.doc_type,
            chapter=payload.chapter,
            title=payload.title,
            issuing_body=payload.issuing_body,
            effective_on=payload.effective_on,
            repealed_on=payload.repealed_on,
            version_label=payload.version_label,
            source_url=payload.source_url,
            is_sample=is_placeholder,
            is_active=True,
            token_count=len(payload.content),
            embedding=embedding,
        )
        session.add(row)
        return row, True

    # 已有权威原文时，不允许被占位记录覆盖
    if not is_placeholder or row.is_sample:
        row.content = payload.content
        row.doc_type = payload.doc_type
        row.chapter = payload.chapter
        row.title = payload.title
        row.issuing_body = payload.issuing_body
        row.effective_on = payload.effective_on
        row.repealed_on = payload.repealed_on
        row.version_label = payload.version_label
        row.source_url = payload.source_url or row.source_url
        row.is_sample = is_placeholder
        row.token_count = len(payload.content)
        if embedding is not None:
            row.embedding = embedding
    return row, False


def _embedding_text(payload: ProvisionInput) -> str:
    return f"《{payload.law_name}》{payload.article_no or ''} {payload.title or ''}\n{payload.content}"


async def ensure_registry(
    session: AsyncSession,
    *,
    embedder: EmbeddingClient | None = None,
    force: bool = False,
) -> int:
    """登记法条占位记录：先补红线引用到的条号，再补法规名级条目。

    `force=False` 时只补缺失，绝不改动已导入的权威原文。
    """
    created = 0

    async def _ensure(law_name: str, article_no: str, doc_type: str, note: str) -> None:
        nonlocal created
        result = await session.execute(
            select(LegalProvision).where(
                LegalProvision.law_name == law_name,
                LegalProvision.article_no == article_no,
            )
        )
        existing = result.scalars().first()
        if existing is not None and not force and not existing.is_sample:
            return
        payload = ProvisionInput(
            law_name=law_name,
            article_no=article_no,
            content=f"{PLACEHOLDER_PREFIX}{note}",
            doc_type=doc_type,
        )
        _, is_new = await upsert_provision(session, payload, embedder=embedder)
        if is_new:
            created += 1

    known_doc_types = dict(_CORE_REGISTRY)

    for law_name, article_no, rule_codes in referenced_provisions():
        doc_type = known_doc_types.get(law_name, KnowledgeDocType.DEPARTMENT_RULE.value)
        note = (
            f"该条为红线规则 {rule_codes} 的引用依据，条号取自规则配置，"
            "导入官方原文前不得作为结论依据展示。"
        )
        await _ensure(law_name, article_no or "全文", doc_type, note)

    for law_name, doc_type in _CORE_REGISTRY:
        await _ensure(
            law_name, "全文", doc_type, "法规级占位登记，需导入逐条原文后才可用于引用。"
        )

    await session.commit()
    logger.info("法条登记完成，新增占位记录 %s 条", created)
    return created


async def rebuild_missing_embeddings(
    session: AsyncSession, embedder: EmbeddingClient, *, limit: int = 200
) -> int:
    """为尚未向量化的权威条文补算向量（占位记录不入库向量）。"""
    result = await session.execute(
        select(LegalProvision)
        .where(
            LegalProvision.embedding.is_(None),
            LegalProvision.is_sample.is_(False),
            LegalProvision.is_active.is_(True),
        )
        .limit(limit)
    )
    rows = list(result.scalars().all())
    if not rows:
        return 0

    texts = [_embedding_text_row(row) for row in rows]
    vectors = await embedder.embed_texts(texts)
    for row, vector in zip(rows, vectors, strict=True):
        row.embedding = vector
    await session.commit()
    logger.info("补算条文向量 %s 条", len(rows))
    return len(rows)


def _embedding_text_row(row: LegalProvision) -> str:
    return f"《{row.law_name}》{row.article_no} {row.title or ''}\n{row.content}"


async def upsert_enforcement_case(
    session: AsyncSession,
    *,
    org_name: str,
    summary: str,
    case_no: str | None = None,
    penalty_body: str | None = None,
    decided_on: object | None = None,
    penalty_content: str = "",
    penalty_amount: float | None = None,
    violation_types: list[str] | None = None,
    basis_refs: list[dict] | None = None,
    source_url: str | None = None,
    is_sample: bool = False,
    embedder: EmbeddingClient | None = None,
) -> EnforcementCase:
    """写入监管罚单，按（机构, 案号）幂等。"""
    stmt = select(EnforcementCase).where(
        EnforcementCase.org_name == org_name,
        EnforcementCase.case_no == case_no,
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        row = EnforcementCase(org_name=org_name, case_no=case_no)
        session.add(row)

    row.summary = summary
    row.penalty_body = penalty_body
    row.decided_on = decided_on
    row.penalty_content = penalty_content
    row.penalty_amount = penalty_amount
    row.violation_types = violation_types or []
    row.basis_refs = basis_refs or []
    row.source_url = source_url
    row.is_sample = is_sample

    if embedder is not None and row.embedding is None and not is_sample:
        text = f"{org_name} {summary} {penalty_content} {' '.join(violation_types or [])}"
        row.embedding = (await embedder.embed_texts([text]))[0]

    await session.commit()
    return row
