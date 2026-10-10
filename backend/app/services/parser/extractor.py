"""文本抽取（保留页码与字符偏移）。

抽取结果必须携带页码与全文偏移，因为后续的溯源高亮要在 PDF.js 上把
风险结论定位回原文具体页与具体字符区间。只返回一整段字符串的设计
会让"高亮"退化成"按文本内容重新搜索"，扫描件与跨页条款下并不可靠。

扫描件 PDF 没有文字层时返回空文本，由上层标记解析失败并提示需要 OCR，
而不是静默产出一份"无风险"报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger(__name__)


class ExtractionError(RuntimeError):
    """文件无法抽取文本。"""


@dataclass(slots=True)
class PageText:
    number: int          # 从 1 开始
    text: str
    char_start: int = 0  # 在拼接后全文中的起始偏移


@dataclass(slots=True)
class ExtractedDocument:
    text: str
    pages: list[PageText] = field(default_factory=list)

    def page_of_offset(self, offset: int) -> int:
        """按全文偏移推断页码。"""
        page = 1
        for candidate in self.pages:
            if offset >= candidate.char_start:
                page = candidate.number
            else:
                break
        return page

    @property
    def page_count(self) -> int:
        return len(self.pages)


def _join_pages(pages: list[tuple[int, str]]) -> ExtractedDocument:
    """拼接分页文本，同时记录每页起始偏移。"""
    parts: list[str] = []
    page_texts: list[PageText] = []
    cursor = 0

    for number, raw in pages:
        cleaned = _normalize_page(raw)
        if not cleaned:
            continue
        if parts:
            cursor += 2  # 页间分隔符 \n\n
            parts.append("\n\n")
        page_texts.append(PageText(number=number, text=cleaned, char_start=cursor))
        parts.append(cleaned)
        cursor += len(cleaned)

    return ExtractedDocument(text="".join(parts), pages=page_texts)


def _normalize_page(raw: str) -> str:
    """去掉行尾空白与连续空行，但保留段落结构。"""
    lines = [line.rstrip() for line in raw.splitlines()]
    kept: list[str] = []
    for line in lines:
        if not line and kept and not kept[-1]:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _extract_pdf(path: Path) -> list[tuple[int, str]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # pragma: no cover - 加密样本难以覆盖
            raise ExtractionError("PDF 已加密，无法读取") from exc

    pages: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            pages.append((index, page.extract_text() or ""))
        except Exception as exc:  # pragma: no cover - 依赖损坏的 PDF
            logger.warning("PDF 第 %s 页抽取失败：%s", index, exc)
            pages.append((index, ""))
    return pages


def _extract_docx(path: Path) -> list[tuple[int, str]]:
    """docx 的页码由渲染时决定，静态解析拿不到可靠分页，统一记为第 1 页。"""
    import docx

    document = docx.Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    # 信贷合同的费用表、还款计划表常放在表格里
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append("\t".join(cells))
    return [(1, "\n".join(parts))]


def _read_plain(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_document(path: Path, suffix: str) -> ExtractedDocument:
    """按后缀抽取文本。"""
    suffix = suffix.lower()

    if suffix == ".pdf":
        document = _join_pages(_extract_pdf(path))
    elif suffix == ".docx":
        document = _join_pages(_extract_docx(path))
    elif suffix in (".txt", ".md"):
        document = _join_pages([(1, _read_plain(path))])
    elif suffix == ".doc":
        raise ExtractionError(".doc 为旧版二进制格式，请先另存为 .docx 后重新上传")
    else:
        raise ExtractionError(f"不支持的文件类型：{suffix}")

    if not document.text.strip():
        raise ExtractionError("未能从文件中抽取到文字，扫描件请先做 OCR 处理")

    # 全角数字与中文标点会干扰数值抽取，统一在抽取阶段归一化
    document.text = _fullwidth_to_halfwidth(document.text)
    document.pages = [
        PageText(number=page.number, text=_fullwidth_to_halfwidth(page.text), char_start=0)
        for page in document.pages
    ]
    return _recompute_page_offsets(document)


_NUM_MAP = str.maketrans("０１２３４５６７８９．％", "0123456789.%")


def _fullwidth_to_halfwidth(text: str) -> str:
    """把全角数字/百分号转成半角，百分号后紧跟数字的写法在合同中很常见。"""
    return text.translate(_NUM_MAP)


def _recompute_page_offsets(document: ExtractedDocument) -> ExtractedDocument:
    """归一化后重新按页文本在全文中定位偏移。"""
    cursor = 0
    for page in document.pages:
        found = document.text.find(page.text, cursor)
        if found >= 0:
            page.char_start = found
            cursor = found + len(page.text)
    return document
