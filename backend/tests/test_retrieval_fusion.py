"""RRF 融合与 jieba 分词。

融合逻辑决定了"哪条法条进提示词"，一旦写错会稳定地把不相关条文喂给模型，
所以只测纯函数，不依赖 ES 与 pgvector。
"""

from __future__ import annotations

from app.services.rag.retriever import _EXACT_REF_BONUS, _RRF_K, ProvisionRetriever
from app.services.rag.sparse import tokenize

fuse = ProvisionRetriever._fuse


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
