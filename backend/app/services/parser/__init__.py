"""合同解析：文本抽取与条款切分。"""

from app.services.parser.extractor import (
    ExtractedDocument,
    ExtractionError,
    PageText,
    extract_document,
)
from app.services.parser.segmenter import batch_segments, locate_excerpt, segmentize

__all__ = [
    "ExtractedDocument",
    "ExtractionError",
    "PageText",
    "batch_segments",
    "extract_document",
    "locate_excerpt",
    "segmentize",
]
