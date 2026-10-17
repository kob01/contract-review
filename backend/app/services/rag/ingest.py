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
from app.services.rag.sparse import SparseDoc, SparseSearchIndex
from app.services.rag.vectorstore import DenseDoc, QdrantVectorStore
from app.services.rules.builtin_redlines import BUILTIN_RED_LINES

logger = get_logger(__name__)

PLACEHOLDER_PREFIX = "【原文待导入】"

#: 规则只引用到"整部法规"级（`basis_refs` 里 `article_no` 为空）时，
#: 占位登记与引用核验都用这个哨兵条号，两侧必须取同一个值。
LAW_LEVEL_ARTICLE_NO = "全文"

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
    session: AsyncSession, payload: ProvisionInput
) -> tuple[LegalProvision, bool]:
    """按（法规名, 条号）写入或更新条文（只写 Postgres 主表）。

    只有 `content` 不是占位前缀开头时才视为权威原文（`is_sample=False`）。
    向量化与 Qdrant/BM25 同步在路由层提交后另行处理（缺向量只告警、不回滚主表写入）。
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
    return row, False


async def ensure_registry(
    session: AsyncSession,
    *,
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
        _, is_new = await upsert_provision(session, payload)
        if is_new:
            created += 1

    known_doc_types = dict(_CORE_REGISTRY)

    for law_name, article_no, rule_codes in referenced_provisions():
        doc_type = known_doc_types.get(law_name, KnowledgeDocType.DEPARTMENT_RULE.value)
        note = (
            f"该条为红线规则 {rule_codes} 的引用依据，条号取自规则配置，"
            "导入官方原文前不得作为结论依据展示。"
        )
        await _ensure(law_name, article_no or LAW_LEVEL_ARTICLE_NO, doc_type, note)

    for law_name, doc_type in _CORE_REGISTRY:
        await _ensure(
            law_name, LAW_LEVEL_ARTICLE_NO, doc_type, "法规级占位登记，需导入逐条原文后才可用于引用。"
        )

    await session.commit()
    logger.info("法条登记完成，新增占位记录 %s 条", created)
    return created


def to_dense_doc(row: LegalProvision, vector: list[float]) -> DenseDoc:
    """把一条已算好向量的条文组装成 Qdrant 待写文档（只带过滤用 payload）。"""
    return DenseDoc(
        record_id=str(row.id),
        vector=vector,
        is_active=row.is_active,
        is_sample=row.is_sample,
        doc_type=row.doc_type,
    )


async def index_vectors(
    session: AsyncSession,
    store: QdrantVectorStore,
    embedder: EmbeddingClient,
    provision_ids: list[str],
) -> int:
    """把指定条文向量化并写入 Qdrant（导入/修改后增量同步）。占位记录不入向量。"""
    if not provision_ids:
        return 0
    import uuid as _uuid

    rows = list(
        (
            await session.execute(
                select(LegalProvision).where(
                    LegalProvision.id.in_([_uuid.UUID(i) for i in provision_ids]),
                    LegalProvision.is_sample.is_(False),
                    LegalProvision.is_active.is_(True),
                )
            )
        ).scalars().all()
    )
    if not rows:
        return 0
    vectors = await embedder.embed_texts([_embedding_text_row(row) for row in rows])
    docs = [to_dense_doc(row, vector) for row, vector in zip(rows, vectors, strict=True)]
    return await store.upsert(store.provisions_collection, docs)


async def sync_vector_store(
    session: AsyncSession,
    store: QdrantVectorStore,
    embedder: EmbeddingClient,
    *,
    batch: int = 64,
) -> int:
    """全量重算并覆盖权威条文向量（换模型/换维或首次从 pgvector 迁移时用）。

    只处理 active 且非占位的条文（占位不入向量），分批 embed→upsert。调用前应已由
    `ensure_collection` 保证集合按当前维度就绪（维度不符会在那里被重建清空）。
    """
    total = 0
    while True:
        rows = list(
            (
                await session.execute(
                    select(LegalProvision)
                    .where(
                        LegalProvision.is_sample.is_(False),
                        LegalProvision.is_active.is_(True),
                    )
                    .order_by(LegalProvision.id)
                    .limit(batch)
                    .offset(total)
                )
            ).scalars().all()
        )
        if not rows:
            break
        vectors = await embedder.embed_texts([_embedding_text_row(row) for row in rows])
        docs = [to_dense_doc(row, vector) for row, vector in zip(rows, vectors, strict=True)]
        await store.upsert(store.provisions_collection, docs)
        total += len(rows)
        if len(rows) < batch:
            break
    logger.info("Qdrant 条文向量同步完成：%s 条", total)
    return total


def to_case_dense_doc(row: EnforcementCase, vector: list[float]) -> DenseDoc:
    return DenseDoc(
        record_id=str(row.id),
        vector=vector,
        is_active=row.is_active,
        is_sample=row.is_sample,
        doc_type="enforcement_case",
    )


def _case_embedding_text(row: EnforcementCase) -> str:
    return f"{row.org_name} {row.summary} {row.penalty_content} {' '.join(row.violation_types or [])}"


async def index_case_vector(
    session: AsyncSession, store: QdrantVectorStore, embedder: EmbeddingClient, case_id: str
) -> int:
    """把一条罚单向量化写入 Qdrant（非占位、active 才入）。"""
    import uuid as _uuid

    row = await session.get(EnforcementCase, _uuid.UUID(case_id))
    if row is None or row.is_sample or not row.is_active:
        return 0
    vector = (await embedder.embed_texts([_case_embedding_text(row)]))[0]
    return await store.upsert(store.cases_collection, [to_case_dense_doc(row, vector)])


async def sync_cases_vector_store(
    session: AsyncSession, store: QdrantVectorStore, embedder: EmbeddingClient, *, batch: int = 64
) -> int:
    """全量重算监管罚单向量（罚单表默认为空，通道关闭时不调用）。"""
    total = 0
    while True:
        rows = list(
            (
                await session.execute(
                    select(EnforcementCase)
                    .where(
                        EnforcementCase.is_sample.is_(False),
                        EnforcementCase.is_active.is_(True),
                    )
                    .order_by(EnforcementCase.id)
                    .limit(batch)
                    .offset(total)
                )
            ).scalars().all()
        )
        if not rows:
            break
        vectors = await embedder.embed_texts([_case_embedding_text(row) for row in rows])
        docs = [to_case_dense_doc(row, vector) for row, vector in zip(rows, vectors, strict=True)]
        await store.upsert(store.cases_collection, docs)
        total += len(rows)
        if len(rows) < batch:
            break
    logger.info("Qdrant 罚单向量同步完成：%s 条", total)
    return total


def _embedding_text_row(row: LegalProvision) -> str:
    return f"《{row.law_name}》{row.article_no} {row.title or ''}\n{row.content}"


def to_sparse_doc(row: LegalProvision) -> SparseDoc:
    return SparseDoc(
        provision_id=str(row.id),
        law_name=row.law_name,
        article_no=row.article_no,
        doc_type=row.doc_type,
        content=row.content,
        is_sample=row.is_sample,
        is_active=row.is_active,
    )


async def index_provisions(
    session: AsyncSession, index: SparseSearchIndex, provision_ids: list[str]
) -> int:
    """把指定条文写进 BM25 索引（导入/修改后增量同步）。"""
    if not provision_ids:
        return 0
    import uuid as _uuid

    rows = (
        await session.execute(
            select(LegalProvision).where(LegalProvision.id.in_([_uuid.UUID(i) for i in provision_ids]))
        )
    ).scalars().all()
    return await index.upsert([to_sparse_doc(row) for row in rows])


async def sync_sparse_index(session: AsyncSession, index: SparseSearchIndex, *, limit: int = 5000) -> int:
    """全量重建 BM25 索引。

    占位记录也进索引：它仍会被召回，但下游会按 `is_sample` 标成“原文待核验”
    并强制转人工，比“进不了检索”更容易暴露缺数据的事实。
    """
    rows = (
        await session.execute(select(LegalProvision).order_by(LegalProvision.id).limit(limit))
    ).scalars().all()
    count = await index.upsert([to_sparse_doc(row) for row in rows])
    logger.info("BM25 索引重建完成：%s 条", count)
    return count


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

    await session.commit()
    return row
