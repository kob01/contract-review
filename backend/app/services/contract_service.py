"""合同解析服务：文件 → 全文 → 条款切片 → 数值指标。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.contract import Contract
from app.models.enums import ContractKind, ContractStatus
from app.services.parser.extractor import ExtractionError, extract_document
from app.services.parser.segmenter import segmentize
from app.services.rules.metrics import extract_metrics

logger = get_logger(__name__)


async def parse_contract(session: AsyncSession, contract: Contract) -> Contract:
    """抽取文本并落库。失败时保留原文件与错误原因，便于换格式重传。

    同时把解析质量观测值写进 `contract.parse_stats`：这些原本只有一行日志，
    进程日志一滚就没，导致 PAR-06/07/11/14（页码归属、失败归因、兜底切片占比、
    解析时延）长期没有数据源。
    """
    contract.status = ContractStatus.PARSING.value
    contract.parse_error = None
    await session.commit()

    started = time.perf_counter()
    stats: dict[str, Any] = {
        "suffix": contract.file_suffix,
        "fileSizeBytes": contract.file_size_bytes,
        "outcome": "failed",
        "timings": {},
    }

    def _finish() -> None:
        stats["timings"]["total"] = round(time.perf_counter() - started, 3)
        # JSON 列必须整体重新赋值，原地改不会被 SQLAlchemy 当成变化
        contract.parse_stats = stats

    path = Path(contract.file_path)
    if not path.exists():
        contract.status = ContractStatus.FAILED.value
        contract.parse_error = f"原文件不存在：{path.name}"
        stats["failureReason"] = "file_missing"
        _finish()
        await session.commit()
        return contract

    step = time.perf_counter()
    try:
        document = extract_document(path, contract.file_suffix)
    except ExtractionError as exc:
        contract.status = ContractStatus.FAILED.value
        contract.parse_error = str(exc)
        stats["failureReason"] = "extraction_error"
        _finish()
        await session.commit()
        logger.warning("合同解析失败 id=%s：%s", contract.id, exc)
        return contract
    stats["timings"]["extract"] = round(time.perf_counter() - step, 3)

    step = time.perf_counter()
    segments = segmentize(document)
    stats["timings"]["segmentize"] = round(time.perf_counter() - step, 3)

    step = time.perf_counter()
    metrics = extract_metrics(document, segments)
    stats["timings"]["metrics"] = round(time.perf_counter() - step, 3)

    contract.content_text = document.text
    contract.char_count = len(document.text)
    contract.page_count = document.page_count
    contract.segments = segments
    contract.extracted_metrics = metrics.to_dict()
    contract.status = ContractStatus.PARSED.value

    # ---------- 解析质量观测 ----------
    #: 兜底切片（标题形如"片段 3（…）"）占比：一份正常合同里这个值高，
    #: 说明标题正则没覆盖它的排版，后续逐批送模型的条款边界就是人造的
    fallback_segments = [
        segment for segment in segments if str(segment.get("title") or "").startswith("片段 ")
    ]
    stats.update(
        {
            "outcome": "parsed",
            "charCount": contract.char_count,
            "pageCount": contract.page_count,
            "segmentCount": len(segments),
            "fallbackSegmentCount": len(fallback_segments),
            "fallbackSegmentRatio": round(len(fallback_segments) / max(1, len(segments)), 4),
            "hasPreamble": bool(segments) and str(segments[0].get("title")) == "合同首部",
            "pagesAttributed": sorted({int(segment.get("page_no") or 1) for segment in segments}),
            "evidenceCount": len(metrics.evidences),
            "derivedMetricCount": len(metrics.derived),
            "metricWarnings": list(metrics.warnings)[:10],
            "flagKeys": sorted(
                {
                    str(evidence.key)
                    for evidence in metrics.evidences
                    if getattr(evidence, "kind", "metric") == "flag"
                }
            ),
        }
    )
    _finish()

    await session.commit()
    logger.info(
        "合同解析完成 id=%s 条款=%s 字符=%s 指标命中=%s 兜底切片=%s 耗时=%ss",
        contract.id,
        len(segments),
        contract.char_count,
        len(metrics.evidences),
        len(fallback_segments),
        stats["timings"]["total"],
    )
    return contract


def infer_contract_kind(text: str) -> str:
    """从正文推断合同种类，供上传时未指定时使用。"""
    lowered = text.lower()
    samples = text[:4000]

    if "担保" in samples and ("抵押" in samples or "质押" in samples):
        return ContractKind.GUARANTEE.value
    if "助贷" in samples or "联合贷" in samples or "导流" in samples:
        return ContractKind.PARTNERSHIP.value
    if "贷款市场报价利率" in samples or "lpr" in lowered or "民间借贷" in samples:
        return ContractKind.PRIVATE_LENDING.value
    if any(word in samples for word in ("借款", "贷款", "授信", "分期")):
        return ContractKind.CONSUMER_LOAN.value

    return ContractKind.UNKNOWN.value
