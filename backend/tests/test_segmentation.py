"""条款切分与原文定位。

高亮能力的全部前提：给定一段原文，能反查出它在第几页、全文哪两个偏移之间。
这两条测试守住的是"引用定位"最容易被悄悄破坏的行为。
"""

from __future__ import annotations

from app.services.parser.segmenter import batch_segments, locate_excerpt, segmentize

from tests.fixtures import CONTRACT_TEXT, build_document


def test_segmentize_splits_by_article_and_keeps_offsets():
    document = build_document()
    segments = segmentize(document)

    assert len(segments) >= 6
    assert segments[0]["title"] == "合同首部"

    for segment in segments:
        # 偏移必须能在全文中还原出对应文本，否则前端高亮会整体错位
        assert segment["text"] in document.text
        assert document.text[segment["char_start"] : segment["char_end"]] == segment["text"]
        assert segment["page_no"] >= 1


def test_multiline_segment_offsets_survive_blank_lines():
    document = build_document()
    segments = segmentize(document)
    target = next(segment for segment in segments if "借款金额与放款" in segment["title"])
    assert "实际放款金额为 95,000 元" in target["text"]
    assert document.text[target["char_start"] : target["char_end"]] == target["text"]


def test_page_number_follows_page_boundaries():
    document = build_document(pages=3)
    segments = segmentize(document)
    pages = {segment["page_no"] for segment in segments}
    assert len(pages) > 1, "跨页合同应当产生多个页码"


def test_locate_excerpt_handles_whitespace_differences():
    document = build_document()
    segments = segmentize(document)

    exact = locate_excerpt(segments, document.text, "综合年化融资成本不超过 36%。")
    assert exact is not None
    assert exact["segment_index"] == 3

    # 抽取文本里的换行/空格差异不应导致定位失败
    compact = locate_excerpt(segments, document.text, "综合年化融资成本不超过36%。")
    assert compact is not None
    assert compact["char_start"] == exact["char_start"]

    assert locate_excerpt(segments, document.text, "本合同不存在的一句话") is None


def test_batch_segments_respects_char_budget():
    document = build_document()
    segments = segmentize(document)

    batches = batch_segments(segments, max_chars=120)
    assert len(batches) > 1
    for batch in batches:
        # 单条超长时允许独占一批，但多条时不得超过预算
        if len(batch) > 1:
            total = sum(len(item["text"]) + len(item["title"]) for item in batch)
            assert total <= 120 + max(len(item["text"]) for item in batch)


def test_segmentize_empty_text():
    assert segmentize(build_document("")) == []


def test_fallback_when_no_heading_markers():
    text = "这是一份没有任何条款编号的协议。双方约定利率按月结算。"
    segments = segmentize(build_document(text))
    assert len(segments) == 1
    assert segments[0]["text"] in text
    assert CONTRACT_TEXT
