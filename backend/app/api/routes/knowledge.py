"""知识库接口：条文导入与检索、罚单管理、统计、监管信息采集。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Settings, build_retriever, get_db, get_settings
from app.api.mappers import to_case_out, to_provision_out
from app.core.logging import get_logger
from app.models.enums import KnowledgeDocType
from app.models.knowledge import EnforcementCase, KnowledgeIngestRun, LegalProvision
from app.models.redline import RedLineRule
from app.schemas.common import PageMeta
from app.schemas.knowledge import (
    EnforcementCaseIn,
    EnforcementCaseOut,
    IngestRunOut,
    IngestRunRequest,
    KnowledgeStats,
    ProvisionIn,
    ProvisionOut,
    ProvisionSearchResponse,
    ReindexResult,
)
from app.services.ingestion.regulator import fetch_penalty_notices, source_available
from app.services.rag.embeddings import (
    EmbeddingClient,
    describe_embedding_backend,
    shared_embedder,
)
from app.services.rag.ingest import (
    PLACEHOLDER_PREFIX,
    ProvisionInput,
    index_provisions,
    rebuild_all_embeddings,
    rebuild_missing_embeddings,
    sync_sparse_index,
    upsert_enforcement_case,
    upsert_provision,
)
from app.services.rag.sparse import shared_sparse_index

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
logger = get_logger(__name__)


def _embedder(app_settings: Settings) -> EmbeddingClient:
    """向量客户端走进程级单例。

    以前每个知识接口请求都 `build_embedding_client` 新建一个 httpx/OpenAI 客户端
    且不关闭，包括导入、搜索、重建这几个入口；同一份配置应该只建一份连接池。
    """
    return shared_embedder(app_settings)


@router.get("/provisions", response_model=list[ProvisionOut])
async def list_provisions(
    law_name: str | None = None,
    doc_type: KnowledgeDocType | None = None,
    only_unimported: bool = Query(
        default=False, description="只看尚未导入权威原文的占位登记"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db),
) -> list[ProvisionOut]:
    stmt = select(LegalProvision).order_by(LegalProvision.law_name, LegalProvision.article_no)
    if law_name:
        stmt = stmt.where(LegalProvision.law_name.ilike(f"%{law_name}%"))
    if doc_type is not None:
        stmt = stmt.where(LegalProvision.doc_type == doc_type.value)
    if only_unimported:
        stmt = stmt.where(LegalProvision.is_sample.is_(True))

    rows = await session.execute(stmt.limit(limit))
    return [to_provision_out(row) for row in rows.scalars().all()]


@router.post("/provisions", response_model=ProvisionOut, status_code=status.HTTP_201_CREATED)
async def import_provision(
    payload: ProvisionIn,
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> ProvisionOut:
    """导入权威条文原文。

    系统不会替你补条文：占位记录（`【原文待导入】`开头）会被标记为未核验，
    引用到它的结论强制转人工。导入正式原文后，同一（法规名, 条号）即视为已核验。
    """
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="content 不能为空")
    if content.startswith(PLACEHOLDER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"content 不能以 {PLACEHOLDER_PREFIX} 开头，那是占位记录的前缀",
        )

    embedder = None
    try:
        embedder = _embedder(app_settings)
    except Exception as exc:  # 向量不可用时仍允许导入原文，检索退回其余通道
        logger.warning("向量客户端不可用，条文将暂不向量化：%s", exc)

    row, _ = await upsert_provision(
        session,
        ProvisionInput(
            law_name=payload.law_name.strip(),
            article_no=payload.article_no.strip(),
            content=content,
            doc_type=payload.doc_type.value,
            chapter=payload.chapter,
            title=payload.title,
            issuing_body=payload.issuing_body,
            effective_on=payload.effective_on,
            repealed_on=payload.repealed_on,
            version_label=payload.version_label,
            source_url=payload.source_url,
        ),
        embedder=embedder,
    )
    await session.commit()

    # 同步 BM25 索引：失败不回滚已落库的条文，但必须说出口，否则新条文只在向量命里
    sparse = shared_sparse_index(app_settings)
    if sparse is not None:
        try:
            await index_provisions(session, sparse, [str(row.id)])
        except Exception as exc:
            logger.warning("条文已入库但未同步到 BM25 索引（可调 POST /knowledge/reindex 补）：%s", exc)

    return to_provision_out(row)


@router.delete("/provisions/{provision_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provision(
    provision_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> None:
    row = await session.get(LegalProvision, provision_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="条文不存在")
    await session.delete(row)
    await session.commit()

    sparse = shared_sparse_index(app_settings)
    if sparse is not None:
        await sparse.delete(str(provision_id))


@router.get("/search", response_model=ProvisionSearchResponse)
async def search_provisions(
    q: str = Query(min_length=2, description="自然语言查询或《法规名》第X条"),
    top_k: int | None = Query(default=None, ge=1, le=30),
    include_inactive: bool = False,
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> ProvisionSearchResponse:
    """混合检索自检接口：各召回通道是什么、命中了什么都可观察。"""
    retriever = build_retriever(session, _embedder(app_settings), app_settings)
    items = await retriever.search(q, top_k=top_k, only_active=not include_inactive)

    match_kinds: list[str] = list(retriever.channels_used)
    for item in items:
        for kind in item.match_kinds:
            if kind not in match_kinds:
                match_kinds.append(kind)

    return ProvisionSearchResponse(
        query=q,
        items=[
            ProvisionOut.model_validate(
                {
                    "id": item.provision_id,
                    "law_name": item.law_name,
                    "article_no": item.article_no,
                    "content": item.content,
                    "doc_type": item.doc_type,
                    "is_sample": item.is_sample,
                    "is_active": True,
                    "has_embedding": True,
                    "effective_on": item.effective_on,
                    "repealed_on": item.repealed_on,
                    "source_url": item.source_url,
                }
            )
            for item in items
        ],
        match_kinds=match_kinds,
        embedding_provider=app_settings.embedding_model,
        embedding_is_mock=app_settings.rag_use_mock_embedding,
        meta=PageMeta(total=len(items), page=1, page_size=len(items) or 1),
    )


@router.post("/reindex", response_model=ReindexResult)
async def reindex_knowledge(
    full: bool = Query(
        False,
        description="true = 全量重算所有权威条文向量（换嵌入模型时用）；false = 只补缺失向量",
    ),
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> ReindexResult:
    """重建检索侧数据：补算/全量重算向量 + 全量同步 BM25 索引。

    换嵌入模型、调整分词口径、ES 索引被清过之后都该调一次。**换嵌入模型必须带
    `full=true`**：同维度换模型时旧的异源向量非 NULL，只补缺失会漏掉它们，导致
    新旧向量混空间、相似度失真。向量与索引分开返回，通道不可用时写进 message，
    而不是伪装成“重建成功”。
    """
    notes: list[str] = []

    embeddings_rebuilt = 0
    try:
        embedder = _embedder(app_settings)
        embeddings_rebuilt = (
            await rebuild_all_embeddings(session, embedder)
            if full
            else await rebuild_missing_embeddings(session, embedder)
        )
    except Exception as exc:
        notes.append(f"向量重建未完成：{exc}")

    sparse = shared_sparse_index(app_settings)
    sparse_synced = 0
    sparse_available = False
    if sparse is None:
        notes.append("BM25 通道已由 RAG_SPARSE_ENABLED 关闭")
    else:
        try:
            sparse_synced = await sync_sparse_index(session, sparse)
            sparse_available = True
        except Exception as exc:
            notes.append(f"BM25 索引未同步：{exc}")

    return ReindexResult(
        embeddings_rebuilt=embeddings_rebuilt,
        sparse_synced=sparse_synced,
        sparse_enabled=app_settings.rag_sparse_enabled,
        sparse_available=sparse_available,
        message="; ".join(notes),
    )


@router.get("/cases", response_model=list[EnforcementCaseOut])
async def list_cases(
    keyword: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_db),
) -> list[EnforcementCaseOut]:
    stmt = select(EnforcementCase).order_by(EnforcementCase.decided_on.desc().nullslast())
    if keyword:
        pattern = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                EnforcementCase.org_name.ilike(pattern),
                EnforcementCase.summary.ilike(pattern),
            )
        )
    rows = await session.execute(stmt.limit(limit))
    return [to_case_out(row) for row in rows.scalars().all()]


@router.post("/cases", response_model=EnforcementCaseOut, status_code=status.HTTP_201_CREATED)
async def import_case(
    payload: EnforcementCaseIn,
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> EnforcementCaseOut:
    embedder = None
    try:
        embedder = _embedder(app_settings)
    except Exception:  # pragma: no cover - 向量不可用时仍可入库
        embedder = None

    row = await upsert_enforcement_case(
        session,
        org_name=payload.org_name,
        summary=payload.summary,
        case_no=payload.case_no,
        penalty_body=payload.penalty_body,
        decided_on=payload.decided_on,
        penalty_content=payload.penalty_content,
        penalty_amount=payload.penalty_amount,
        violation_types=payload.violation_types,
        basis_refs=payload.basis_refs,
        source_url=payload.source_url,
        embedder=embedder,
    )
    return to_case_out(row)


@router.get("/stats", response_model=KnowledgeStats)
async def knowledge_stats(
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> KnowledgeStats:
    provision_total = await session.scalar(select(func.count()).select_from(LegalProvision))
    placeholder = await session.scalar(
        select(func.count())
        .select_from(LegalProvision)
        .where(LegalProvision.is_sample.is_(True))
    )
    case_total = await session.scalar(select(func.count()).select_from(EnforcementCase))
    rule_total = await session.scalar(select(func.count()).select_from(RedLineRule))
    rule_active = await session.scalar(
        select(func.count()).select_from(RedLineRule).where(RedLineRule.is_active)
    )

    total = int(provision_total or 0)
    samples = int(placeholder or 0)
    return KnowledgeStats(
        provision_total=total,
        provision_imported=total - samples,
        provision_placeholder=samples,
        case_total=int(case_total or 0),
        rule_total=int(rule_total or 0),
        rule_active=int(rule_active or 0),
        embedding_provider=(
            "mock_hashing" if app_settings.rag_use_mock_embedding else describe_embedding_backend(app_settings)
        ),
        embedding_is_mock=app_settings.rag_use_mock_embedding,
    )


@router.post("/ingest", response_model=IngestRunOut)
async def trigger_ingest(
    payload: IngestRunRequest,
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> IngestRunOut:
    """采集监管公开处罚信息。

    三重闸门：配置开关 `INGESTION_ENABLED`、来源可用、调用方显式确认已核对
    目标站点的 robots 与使用条款。任一不满足直接拒绝，不静默跳过。
    """
    if not app_settings.ingestion_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="采集未启用，请设置 INGESTION_ENABLED=true 并确认使用条款后重试",
        )
    if not payload.confirmed_compliance_ok:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="必须显式确认已核对目标站点的 robots.txt 与使用条款",
        )
    if not source_available(payload.source):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"未实现或不支持的信息来源：{payload.source}",
        )

    outcome = await fetch_penalty_notices(
        session, source=payload.source, settings=app_settings, limit=payload.limit
    )

    run = KnowledgeIngestRun(
        source=payload.source,
        doc_type=KnowledgeDocType.ENFORCEMENT_CASE.value,
        item_count=outcome["item_count"],
        ok_count=outcome["ok_count"],
        skipped_count=outcome["skipped_count"],
        status="succeeded" if not outcome["errors"] else "partial",
        message="; ".join(outcome["errors"])[:2000] or None,
    )
    session.add(run)
    await session.commit()

    return IngestRunOut.model_validate(
        {
            "id": run.id,
            "source": run.source,
            "doc_type": run.doc_type,
            "item_count": run.item_count,
            "ok_count": run.ok_count,
            "skipped_count": run.skipped_count,
            "status": run.status,
            "message": run.message,
            "created_at": run.created_at,
        }
    )
