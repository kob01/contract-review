"""合同解析服务：文件 → 全文 → 条款切片 → 数值指标。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.contract import Contract
from app.models.enums import ContractKind, ContractStatus
from app.services.parser.extractor import ExtractionError, extract_document
from app.services.parser.segmenter import segmentize
from app.services.rules.metrics import extract_metrics

logger = get_logger(__name__)


async def parse_contract(session: AsyncSession, contract: Contract) -> Contract:
    """抽取文本并落库。失败时保留原文件与错误原因，便于换格式重传。"""
    contract.status = ContractStatus.PARSING.value
    contract.parse_error = None
    await session.commit()

    path = Path(contract.file_path)
    if not path.exists():
        contract.status = ContractStatus.FAILED.value
        contract.parse_error = f"原文件不存在：{path.name}"
        await session.commit()
        return contract

    try:
        document = extract_document(path, contract.file_suffix)
    except ExtractionError as exc:
        contract.status = ContractStatus.FAILED.value
        contract.parse_error = str(exc)
        await session.commit()
        logger.warning("合同解析失败 id=%s：%s", contract.id, exc)
        return contract

    segments = segmentize(document)
    metrics = extract_metrics(document, segments)

    contract.content_text = document.text
    contract.char_count = len(document.text)
    contract.page_count = document.page_count
    contract.segments = segments
    contract.extracted_metrics = metrics.to_dict()
    contract.status = ContractStatus.PARSED.value

    await session.commit()
    logger.info(
        "合同解析完成 id=%s 条款=%s 字符=%s 指标命中=%s",
        contract.id,
        len(segments),
        contract.char_count,
        len(metrics.evidences),
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
