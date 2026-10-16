"""RRF 融合、jieba 分词与词面信号。

融合逻辑决定了"哪条法条进提示词"，一旦写错会稳定地把不相关条文喂给模型，
所以只测纯函数，不依赖 ES 与向量库。
"""

from __future__ import annotations

from app.services.rag.retriever import (
    _EXACT_REF_BONUS,
    _RRF_K,
    ProvisionRetriever,
    RetrievedProvision,
    lexical_overlap,
)
from app.services.rag.sparse import tokenize

fuse = ProvisionRetriever._fuse


def make_provision(provision_id: str, law_name: str, *, is_sample: bool = False, score: float = 0.5):
    return RetrievedProvision(
        provision_id=provision_id,
        law_name=law_name,
        article_no="第二十一条",
        content="示例原文",
        doc_type="law",
        is_sample=is_sample,
        score=score,
        match_kinds=["vector"],
    )


def test_exact_ref_always_leads():
    """规则库声明的引用依据必须压过任何名次奖励。"""
    fused = fuse({"vector": ["b", "a"], "exact_ref": ["c"]})
    assert fused["c"] >= _EXACT_REF_BONUS
    assert fused["c"] > max(fused["a"], fused["b"])


def test_multi_channel_hit_outranks_single_channel():
    """两路都命中第 2 名，应高于一路命中第 1 名：交叉确认可信度更高。"""
    fused = fuse({"vector": ["a", "b"], "bm25": ["b"]})
    assert fused["b"] > fused["a"]


def test_rrf_uses_rank_not_raw_score():
    """同一通道内名次越靠前加分越多，且加分只与名次有关。"""
    fused = fuse({"vector": ["x", "y", "z"]})
    assert fused["x"] == 1 / (_RRF_K + 1)
    assert fused["y"] == 1 / (_RRF_K + 2)
    assert fused["z"] == 1 / (_RRF_K + 3)
    assert fused["x"] > fused["y"] > fused["z"]


def test_unknown_channel_does_not_leak_scores():
    """直连之外的通道不应互相污染：未出现的 id 不该有分。"""
    fused = fuse({"vector": ["a"], "keyword": ["b"]})
    assert set(fused) == {"a", "b"}


def test_tokenize_keeps_legal_terms_and_drops_noise():
    tokens = tokenize("借款的利息不得预先在本金中扣除，年化利率24%")
    assert "利息" in tokens
    assert "本金" in tokens
    assert "预先" in tokens
    # 停用词与单个汉字被过滤，避免"的/了"这类词主导 BM25
    assert "的" not in tokens
    assert "不" not in tokens


def test_tokenize_keeps_ascii_tokens_lowercase():
    tokens = tokenize("按 LPR 的四倍计算")
    assert "lpr" in tokens


# ---------- 词面信号：分词集合而不是逐字符比对 ----------


def test_lexical_overlap_scores_token_sets_not_characters():
    """“民法”与“民法典”在字符集合里会重复计分，分词后才是同一个词。"""
    item = make_provision("p1", "中华人民共和国民法典")
    index = {"p1": {"中华人民共和国", "民法典"}}

    assert lexical_overlap(item, {"民法典", "利率"}, index) == 0.5
    assert lexical_overlap(item, {"民法典", "中华人民共和国"}, index) == 1.0
    assert lexical_overlap(item, set(), index) == 0.0
    # 没建索引的条目不参与词面加分，也不能报错
    assert lexical_overlap(item, {"民法典"}, {}) == 0.0


def test_rank_provisions_prefers_real_text_then_lexical_match():
    from app.services.review_pipeline import _rank_provisions

    items = [
        make_provision("sample", "中华人民共和国民法典", is_sample=True, score=0.9),
        make_provision("real-other", "银行保险机构消费者权益保护管理办法", score=0.6),
        make_provision("real-civil", "中华人民共和国民法典", score=0.55),
    ]
    index = {
        "sample": {"中华人民共和国", "民法典"},
        "real-other": {"银行", "保险机构", "消费者", "权益保护", "管理办法"},
        "real-civil": {"中华人民共和国", "民法典"},
    }
    ranked = _rank_provisions(items, {"民法典", "借款", "利率"}, index)

    # 占位登记排最后（即使分数最高）：合成文本不能挤掉真实原文
    assert ranked[-1].provision_id == "sample"
    # 同为非占位时，词面命中的那条应该赢
    assert ranked[0].provision_id == "real-civil"


def test_batch_text_covers_titles_and_body_prefix():
    from app.services.review_pipeline import _batch_text

    text = _batch_text([{"index": 1, "title": "第二条 利率", "text": "日利率万分之五" * 100}])
    assert "第二条 利率" in text
    assert len(text) < 400, "每批只取正文前若干字，不能把整批原文全量再拼一遍"
