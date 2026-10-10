"""引用核验（Citation Verification）。

模型给出的"依据《XX 办法》第 X 条"是不可信的自述，必须回到知识库比对：
- 命中且已有权威原文 → `verified=True`，前端可正常展示；
- 命中但只有占位登记 → `verified=False`，原因"原文未导入"；
- 未命中 → `verified=False`，原因"知识库无此条文"，几乎必然是模型编造。

任何带未核验引用的结论都会被强制转人工复核，这是防幻觉的最后一道闸。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.knowledge import LegalProvision
from app.services.rag.retriever import RetrievedProvision

logger = get_logger(__name__)


def _normalize_law_name(name: str) -> str:
    """去掉书名号与空白，容忍《民法典》与 中华人民共和国民法典 的差异。"""
    cleaned = (name or "").strip().strip("《》")
    return cleaned.replace(" ", "")


async def resolve_citations(
    session: AsyncSession,
    raw_citations: list[dict[str, Any]],
) -> tuple[list[dict], list[str]]:
    """校验模型给出的引用。

    返回 `(citations, unverified_reasons)`，citations 结构与
    `ReviewFinding.citations` 一致（camelCase，可直接给前端）。
    """
    citations: list[dict] = []
    reasons: list[str] = []

    for raw in raw_citations:
        law_name = _normalize_law_name(str(raw.get("law_name") or raw.get("lawName") or ""))
        article_no = str(raw.get("article_no") or raw.get("articleNo") or "").strip()
        provision_id = str(raw.get("provision_id") or raw.get("provisionId") or "").strip()

        row = await _lookup(session, law_name, article_no, provision_id)
        if row is None:
            reason = f"知识库未收录《{law_name or '未知法规'}》{article_no}"
            reasons.append(reason)
            citations.append(
                {
                    "provisionId": provision_id or None,
                    "lawName": law_name,
                    "articleNo": article_no or None,
                    "quote": str(raw.get("quote") or ""),
                    "docType": str(raw.get("docType") or raw.get("doc_type") or ""),
                    "isSample": True,
                    "verified": False,
                    "verifyReason": reason,
                    "sourceUrl": None,
                }
            )
            continue

        record = RetrievedProvision(
            provision_id=str(row.id),
            law_name=row.law_name,
            article_no=row.article_no,
            content=row.content,
            doc_type=row.doc_type,
            is_sample=row.is_sample,
            score=1.0,
            match_kinds=["citation_check"],
            effective_on=row.effective_on.isoformat() if row.effective_on else None,
            repealed_on=row.repealed_on.isoformat() if row.repealed_on else None,
            source_url=row.source_url,
        )

        citation = record.to_citation(verified=not record.is_sample)
        if record.is_sample:
            reason = f"《{record.law_name}》{record.article_no} 仅有占位登记，原文未导入"
            reasons.append(reason)
            citation["verifyReason"] = reason
        citations.append(citation)

    return citations, reasons


async def _lookup(
    session: AsyncSession,
    law_name: str,
    article_no: str,
    provision_id: str,
) -> LegalProvision | None:
    """按主键优先、其次（法规名, 条号）查找。"""
    if provision_id:
        try:
            row = await session.get(LegalProvision, uuid.UUID(provision_id))
        except (ValueError, TypeError):
            row = None
        if row is not None:
            return row

    if not law_name:
        return None

    stmt = select(LegalProvision).where(_law_name_match(law_name))
    if article_no:
        stmt = stmt.where(LegalProvision.article_no == article_no)
    result = await session.execute(stmt.limit(1))
    return result.scalars().first()


def _law_name_match(law_name: str):
    """支持简称命中全称：模型常写《民法典》，库里存的是《中华人民共和国民法典》。"""
    short = law_name.replace("中华人民共和国", "")
    clauses = [LegalProvision.law_name == law_name]
    if short != law_name:
        clauses.append(LegalProvision.law_name.like(f"%{short}"))
    else:
        clauses.append(LegalProvision.law_name == short)
    return or_(*clauses)


def merge_retrieved_context(
    citations: list[dict], retrieved: list[RetrievedProvision]
) -> list[dict]:
    """把检索到但模型没引用的法条也带在响应里，作为"审查参考依据"展示。"""
    cited_ids = {item.get("provisionId") for item in citations if item.get("provisionId")}
    extras: list[dict] = []
    for item in retrieved:
        if item.provision_id in cited_ids:
            continue
        extras.append(item.to_citation(verified=not item.is_sample))
    return extras
