"""条款切分（携带全文偏移与页码）。

产出的每个片段形如：

    {"index": 3, "title": "第三条 利率与费用", "text": "...",
     "char_start": 812, "char_end": 1630, "page_no": 2}

偏移用于把规则引擎/模型给出的原文引用定位回 PDF 的具体页与具体区间，
是"溯源高亮"能力的地基。
"""

from __future__ import annotations

import re

from app.services.parser.extractor import ExtractedDocument

#: 依次尝试的条款标题样式，命中即视为新条款开始
_HEADING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百零〇]+\s*[条章节编部篇]\s*[（(]?"),
    re.compile(r"^\s*[（(]?\s*[0-9一二三四五六七八九十]+\s*[）)、\.．]\s*"),
    re.compile(r"^\s*\d+(?:\.\d+){1,3}\s*[、\.．]?\s*"),
    re.compile(r"^\s*(?:附件|附录|附表)\s*[0-9一二三]*"),
)

#: 标题行通常很短，超长说明这只是普通陈述句
_MAX_HEADING_CHARS = 40

#: 未识别出任何标题时的兜底分段长度
_FALLBACK_CHUNK_CHARS = 2_000


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return False
    return any(pattern.match(stripped) for pattern in _HEADING_PATTERNS)


def _clean_title(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip()).rstrip("：:，,。")


def _make_segment(index: int, title: str, text: str, char_start: int, doc: ExtractedDocument) -> dict:
    char_end = char_start + len(text)
    return {
        "index": index,
        "title": title,
        "text": text,
        "char_start": char_start,
        "char_end": char_end,
        "page_no": doc.page_of_offset(char_start),
    }


def _fallback_segments(doc: ExtractedDocument) -> list[dict]:
    """没有任何标题特征时，按空行聚合成大致等长的片段。"""
    blocks = re.split(r"\n{2,}", doc.text)
    segments: list[dict] = []
    cursor = 0
    buffer: list[str] = []
    buffer_start = 0
    size = 0

    for block in blocks:
        found = doc.text.find(block, cursor)
        if found < 0:
            continue
        cursor = found + len(block)
        if not buffer:
            buffer_start = found
        buffer.append(block)
        size += len(block)
        if size >= _FALLBACK_CHUNK_CHARS:
            segments.append((buffer_start, "\n\n".join(buffer)))
            buffer, size = [], 0

    if buffer:
        segments.append((buffer_start, "\n\n".join(buffer)))

    result: list[dict] = []
    for number, (start, body) in enumerate(segments, start=1):
        head = body.splitlines()[0][:20] if body else ""
        title = f"片段 {number}" + (f"（{head}…）" if head else "")
        result.append(_make_segment(number, title, body, start, doc))
    return result


def segmentize(doc: ExtractedDocument) -> list[dict]:
    """把合同全文切分为条款片段。

    首个标题之前的内容记为"合同首部"，便于审查主体资格、放款资质与签署信息。
    """
    if not doc.text.strip():
        return []

    segments: list[dict] = []
    current_title: str | None = None
    current_start = 0
    buffer: list[str] = []
    offset = 0

    def flush() -> None:
        """收尾当前条款。标题行同样保留在正文里，保证偏移与全文一致。"""
        nonlocal buffer, current_title
        body = "\n".join(buffer).strip()
        if body:
            segments.append(
                _make_segment(len(segments) + 1, current_title or "", body, current_start, doc)
            )
        buffer, current_title = [], None

    for line in doc.text.splitlines(keepends=True):
        stripped = line.strip()
        is_heading = _is_heading(stripped)

        if is_heading and buffer:
            flush()

        if is_heading:
            current_start = offset
            current_title = _clean_title(stripped)
        elif not buffer and stripped:
            current_start = offset

        if stripped or buffer:
            buffer.append(line.rstrip())
        offset += len(line)

    flush()

    if not segments:
        return _fallback_segments(doc)

    # 只有一个片段且没有标题，说明标题特征完全没命中
    if len(segments) == 1 and not segments[0]["title"]:
        return _fallback_segments(doc)

    if not segments[0]["title"]:
        segments[0]["title"] = "合同首部"

    for number, segment in enumerate(segments, start=1):
        segment["index"] = number
    return segments


def batch_segments(segments: list[dict], max_chars: int) -> list[list[dict]]:
    """按字符预算把条款打包成若干批次，单条超长时独占一批。"""
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0

    for segment in segments:
        length = len(segment["text"]) + len(segment["title"])
        if current and size + length > max_chars:
            batches.append(current)
            current, size = [], 0
        current.append(segment)
        size += length

    if current:
        batches.append(current)
    return batches


def locate_excerpt(segments: list[dict], full_text: str, excerpt: str) -> dict | None:
    """把一段原文引用定位回 `{"segment_index","char_start","char_end","page_no","method"}`。

    模型返回的引用常带空白差异，因此先做精确匹配，失败后退化为
    "去空白后再匹配"，最后只能定位到条款级；完全找不到则返回 None，
    由上层标注为"引用未定位"，绝不假装定位成功。

    `method` 是评测埋点：精确匹配与模糊匹配定位上的结论，在 PDF 抽取质量差时的
    错位风险不同，必须能分开统计（GEN-12）。
    """
    needle = excerpt.strip()
    if not needle:
        return None

    hit = full_text.find(needle)
    if hit >= 0:
        return _wrap_position(segments, full_text, hit, hit + len(needle), method="exact")

    compact = _strip_ws(full_text)
    compact_needle = _strip_ws(needle)
    compact_hit = compact.find(compact_needle)
    if compact_hit >= 0:
        # 压缩后的偏移映射回原始偏移
        start = _map_compact_offset(full_text, compact_hit)
        end = _map_compact_offset(full_text, compact_hit + len(compact_needle))
        return _wrap_position(segments, full_text, start, end, method="normalized")

    return None


_WS_RE = re.compile(r"\s+")


def _strip_ws(text: str) -> str:
    return _WS_RE.sub("", text)


def _map_compact_offset(full_text: str, compact_index: int) -> int:
    """把"去掉所有空白后的下标"映射回原文下标。"""
    seen = 0
    for index, char in enumerate(full_text):
        if char.isspace():
            continue
        if seen == compact_index:
            return index
        seen += 1
    return len(full_text)


def _wrap_position(
    segments: list[dict], full_text: str, start: int, end: int, *, method: str = "exact"
) -> dict:
    matched_index: int | None = None
    for segment in segments:
        if segment["char_start"] <= start < segment["char_end"]:
            matched_index = segment["index"]
            break
    if matched_index is None and segments:
        # 跨条款引用时归入起始条款
        matched_index = segments[0]["index"]
        for segment in segments:
            if segment["char_start"] <= start:
                matched_index = segment["index"]

    return {
        "segment_index": matched_index,
        "char_start": start,
        "char_end": end,
        "method": method,
        "page_no": next(
            (
                segment["page_no"]
                for segment in segments
                if segment["index"] == matched_index
            ),
            1,
        ),
    }
