"""指标库与评测埋点的单元测试。

这些数字是评测报告的地基，所以断言全部用**手算期望值**，不是"跑一遍把输出抄进去"：
抄输出的测试永远绿，哪怕指标本身算错了。每个用例都注明期望值怎么来的。

覆盖范围刻意集中在两类最容易出错的地方：
1. 分母（哪些条目进统计、哪些算"未测"）；
2. 召回优先口径（F2、漏报清单、逃逸率）——这些数字方向搞反了，
   报告会把"漏报严重"显示成"质量不错"。
"""

from __future__ import annotations

from pathlib import Path

from app.models.enums import CitationVerification, LocateMethod, ModelSlot
from app.services.eval.calibration import (
    Sample,
    auroc,
    brier_score,
    escape_rate,
    expected_calibration_error,
    reliability_table,
    sweep_threshold,
)
from app.services.eval.gold import GoldCase, GoldFinding, GoldRef, GoldSpan, _resolve_fixture
from app.services.eval.metrics import (
    ClassScore,
    citation_coverage,
    classification_report,
    counting_invariants,
    jaccard,
    percentiles,
    self_consistency,
    span_hit,
)
from app.services.eval.retrieval import (
    channel_report,
    hit_rate_at_k,
    missing_gold,
    mrr,
    ndcg_at_k,
    recall_at_k,
)
from app.services.telemetry import (
    StageTimer,
    citation_verification_status,
    config_fingerprint,
    locate_method_name,
    summarize_token_usage,
)

# ---------- 分类指标（RULE-01 / GEN-03）----------


def test_classification_counts_and_f_beta_direction():
    """手算：期望 {A,B}，预测 {A,C} → A=TP，B=FN，C=FP；micro P=R=0.5。

    F1 = 2·0.5·0.5/(0.5+0.5) = 0.5，F2 = 5·0.5·0.5/(4·0.5+0.5) = 0.5
    —— P=R 时两者相等，这是 F-beta 公式没写错的最短检验。
    """
    report = classification_report([["A", "B"]], [["A", "C"]])
    assert report.per_label["A"].true_positives == 1
    assert report.per_label["B"].false_negatives == 1
    assert report.per_label["C"].false_positives == 1
    assert report.micro_precision == 0.5
    assert report.micro_recall == 0.5
    assert report.micro_f1 == 0.5
    assert report.micro_f2 == 0.5

    # micro 把"整条红线一个没中"平均掉了：4 个期望中 1 个 → micro 召回 0.25，
    # 但 B/C/D 三条的召回是 0.0。worst_recall 存在的意义就是不让这种分布拿高分
    starved = classification_report([["A", "B", "C", "D"]], [["A"]])
    assert starved.micro_recall == 0.25
    assert starved.worst_recall[1] == 0.0
    assert starved.worst_recall[0] in {"B", "C", "D"}
    # P=1、R=0.25 → F1=0.4，F2=5·0.25/(4+0.25)=0.2941
    # 召回吃亏时 F2 更低，正是"用 F2 当主指标会让漏报多的版本更难过关"
    assert starved.micro_f1 == 0.4
    assert starved.micro_f2 == 0.2941

    # 反过来：精确率吃亏（多报）时 F2 高于 F1，说明它确实在替召回说话
    spurious_heavy = classification_report([["A"]], [["A", "B", "C", "D"]])
    assert spurious_heavy.micro_precision == 0.25
    assert spurious_heavy.micro_f1 == 0.4
    assert spurious_heavy.micro_f2 == 0.625


def test_class_score_f_beta_on_perfect_label():
    """全对的一条红线 F1=F2=1.0；漏一条时 F2 必须比 F1 掉得更多。

    TP=4、FN=1 → P=1、R=0.8：F1=2·0.8/1.8=0.8889，F2=5·0.8/(4+0.8)=0.8333。
    F2 更贴近召回，所以"漏报"在 F2 上的代价更大 —— 这就是合规场景选它当主指标的理由。
    """
    score = ClassScore(label="RATE-001", true_positives=4)
    assert (score.precision, score.recall, score.f1, score.f2) == (1.0, 1.0, 1.0, 1.0)
    score.false_negatives = 1
    assert score.f1 == 0.8889
    assert score.f2 == 0.8333
    assert score.f2 < score.f1


def test_classification_reports_labels_that_never_fired():
    """金标准里有、但整个集合一次都没预测出来的标签必须单独点名。

    这是"整条红线 0 召回"的唯一暴露方式：混在 macro 平均值里只会让总分低一点。
    """
    report = classification_report([["RATE-001"], ["RATE-004"]], [["RATE-001"], []])
    assert report.unobserved_labels == ["RATE-004"]


def test_classification_empty_prediction_on_negative_case_is_clean():
    """负例：期望空、预测空 → 没有任何标签可判，不能编出 1.0 的假满分。"""
    report = classification_report([[]], [[]])
    assert report.micro_recall == 0.0
    assert report.micro_precision == 0.0
    assert report.macro_f1 == 0.0


# ---------- span 对齐（GEN-12 / PAR-10）----------


def test_jaccard_and_span_threshold():
    """区间 (0,10) 与 (5,15)：交集 5、并集 15 → Jaccard=1/3 < 0.5，判不命中。"""
    assert abs(jaccard((0, 10), (5, 15)) - 1 / 3) < 1e-9
    assert span_hit((0, 10), (5, 15)) is False
    assert span_hit((0, 10), (0, 12)) is True  # 交集10 并集12 → 0.833
    assert span_hit(None, (0, 10)) is False
    # 偏移缺失时退化到子串判据
    assert span_hit(None, None, gold_excerpt="综合年化 28%", predicted_excerpt="综合年化 28% 以内") is True


# ---------- 引用核验（GEN-06 / GEN-07）----------


def test_citation_verification_status_buckets():
    """四类失败要分开：全占位是知识库欠账，未收录是幻觉嫌疑。"""
    assert citation_verification_status([]) == CitationVerification.NONE.value
    assert citation_verification_status([{"verifyCode": "ok"}]) == CitationVerification.VERIFIED.value
    assert (
        citation_verification_status([{"verifyCode": "ok"}, {"verifyCode": "placeholder"}])
        == CitationVerification.PARTIAL.value
    )
    assert (
        citation_verification_status([{"verifyCode": "placeholder"}])
        == CitationVerification.PLACEHOLDER.value
    )
    assert citation_verification_status([{"verifyCode": "not_in_kb"}]) == CitationVerification.NOT_IN_KB.value
    # 历史数据没有 verifyCode：`isSample=True` 在"占位"和"未收录"两条分支里都会被写下，
    # 从布尔字段无法反推是哪一种，因此按更可疑的 not_in_kb 记（宁可多看一条幻觉，
    # 也不把库里根本没有的引用放成"只是欠账"）。要区分就得重新跑，老数据不做猜测
    assert citation_verification_status([{"verified": False, "isSample": True}]) == (
        CitationVerification.NOT_IN_KB.value
    )
    assert citation_verification_status([{"verified": False}]) == CitationVerification.NOT_IN_KB.value
    assert citation_verification_status([{"verified": True}]) == CitationVerification.VERIFIED.value


def test_citation_coverage_denominator_is_all_citations():
    """已核验率的分母必须是全部引用。

    用"已核验 / 已核验+核验失败"这种自循环口径，会让只有 1 条引用的结论
    和 20 条引用的结论等权，报告里的数字就失去意义。
    """
    findings = [
        {
            "citations": [
                {"verifyCode": "ok"},
                {"verifyCode": "placeholder"},
                {"verifyCode": "not_in_kb"},
            ]
        },
        {"citations": [{"verifyCode": "ok"}, {"verifyCode": "ok"}]},
    ]
    coverage = citation_coverage(findings)
    assert coverage["citationCount"] == 5
    assert coverage["verifiedRate"] == 0.6  # 3/5
    assert coverage["placeholderRate"] == 0.2
    assert coverage["notInKbRate"] == 0.2
    assert coverage["findingWithCitationRate"] == 1.0  # 两条结论各有至少一条已核验引用


# ---------- 报告计数不变式（RPT-01/02/03）----------


def test_counting_invariants_catches_mismatched_counts():
    """报告说 2 条高危、结论里只有 1 条 → 必须报错。"""
    findings = [{"riskLevel": "high"}, {"riskLevel": "medium"}]
    review = {
        "highRiskCount": 2,  # 实际 1
        "mediumRiskCount": 1,
        "lowRiskCount": 0,
        # 100 - (18 + 8) = 74，写成 82 就是复算不一致
        "riskScore": 82,
        "ruleHitCount": 0,
        "pendingHumanCount": 0,
        "summary": "",
    }
    problems = counting_invariants(review, findings)
    assert any("highRiskCount" in item for item in problems)
    assert any("riskScore" in item for item in problems)


def test_counting_invariants_passes_on_consistent_review():
    findings = [{"riskLevel": "high", "source": "rule"}]
    review = {
        "highRiskCount": 1,
        "mediumRiskCount": 0,
        "lowRiskCount": 0,
        "riskScore": 82,
        "ruleHitCount": 1,
        "pendingHumanCount": 0,
        "summary": "",
    }
    assert counting_invariants(review, findings) == []


# ---------- 检索指标（RET-01 / RET-03）----------


def test_recall_mrr_ndcg_hand_computed():
    """查询1：gold={a,b}，排名 [x,a,y,b] → Recall@4=1.0，MRR=1/2。

    查询2：gold={z}，排名 [x,y] → Recall=0，MRR=0。
    → recall@4 = (1.0 + 0) / 2 = 0.5；mrr = (0.5 + 0)/2 = 0.25
    """
    ranked = [["x", "a", "y", "b"], ["x", "y"]]
    gold = [{"a", "b"}, {"z"}]
    assert recall_at_k(ranked, gold, 4) == 0.5
    assert mrr(ranked, gold) == 0.25
    assert hit_rate_at_k(ranked, gold, 1) == 0.0  # 第一名都不相关

    # nDCG@4 查询1：a 在第2位、b 在第4位 → DCG=1/log2(3)+1/log2(5)；IDCG=1/log2(2)+1/log2(3)
    expected = (1 / 1.584962500721156 + 1 / 2.321928094887362) / (
        1 / 1.0 + 1 / 1.584962500721156
    )
    assert abs(ndcg_at_k([ranked[0]], [gold[0]], 4) - expected) < 0.01

    assert missing_gold(ranked, gold, 4) == [{"queryIndex": 1, "missing": ["z"]}]


def test_retrieval_metrics_reject_misaligned_inputs():
    """查询与金标准数量不一致时必须报错，不能悄悄补空集把召回算低。"""
    try:
        recall_at_k([["a"], ["b"]], [{"a"}], 4)
    except ValueError as exc:
        assert "不对齐" in str(exc)
    else:  # pragma: no cover - 断言必须走到异常分支
        raise AssertionError("错位输入没有报错")


def test_channel_report_counts_rerank_kill_of_gold():
    """金标准条文被精排剔除 → 计入误杀；非金标准的剔除不算。"""
    events = [
        {
            "provision_id": "gold-1",
            "batch_no": 1,
            "rank_by_channel": {"vector": 3},
            "dropped_by_rerank": True,
        },
        {
            "provision_id": "gold-2",
            "batch_no": 1,
            "rank_by_channel": {"bm25": 1, "vector": 2},
            "dropped_by_rerank": False,
        },
        {
            "provision_id": "noise",
            "batch_no": 1,
            "rank_by_channel": {"keyword": 5},
            "dropped_by_rerank": True,
        },
    ]
    report = channel_report(events, gold_ids={"gold-1", "gold-2"}, batch_count=2)
    assert report.gold_total == 2
    assert report.rerank_kill.get("dropped") == 1
    # gold-2 被两路同时召回，gold-1 只有 vector 一路 → 独占贡献记在 vector
    assert report.unique_gold_hits.get("vector") == 1
    assert report.participation["bm25"] == 0.5  # 2 批里只有 1 批出现 bm25


# ---------- 校准与路由（RTR-01 / RTR-04）----------


def test_auroc_perfect_and_chance():
    """完美分离 = 1.0；全并列 = 0.5。"""
    perfect = [Sample(0.9, True), Sample(0.8, True), Sample(0.2, False), Sample(0.1, False)]
    assert auroc(perfect) == 1.0
    tied = [Sample(0.5, True), Sample(0.5, False)]
    assert auroc(tied) == 0.5
    assert auroc([Sample(0.5, True)]) is None  # 只有一类，AUC 无定义


def test_ece_rewards_calibration_not_accuracy():
    """校准好 ≠ 准确率高：置信度 0.7 的桶里实际正确率 0.7，误差就该是 0。"""
    # 10 条：7 对 3 错，置信度全 0.7 → 实际正确率 0.7 = 声明置信度 → ECE=0
    samples = [Sample(0.7, index < 7) for index in range(10)]
    assert expected_calibration_error(samples) == 0.0
    assert brier_score(samples) == round((7 * 0.09 + 3 * 0.49) / 10, 4)

    # 同样 70% 准确率，但全部自称 1.0 把握 → ECE=0.3
    overconfident = [Sample(1.0, index < 7) for index in range(10)]
    assert expected_calibration_error(overconfident) == 0.3


def test_escape_rate_counts_wrong_and_auto_passed():
    """逃逸 = 判错了 yet 置信度达到阈值被自动放过。"""
    samples = [
        Sample(0.95, False, label="should-have-been-caught"),
        Sample(0.95, True),
        Sample(0.50, False),  # 低于阈值，会转人工，不算逃逸
    ]
    result = escape_rate(samples, threshold=0.80)
    assert result["count"] == 1
    assert result["rate"] == round(1 / 3, 4)
    assert result["items"][0]["label"] == "should-have-been-caught"
    assert escape_rate([], threshold=0.8)["measured"] is False


def test_sweep_threshold_pairs_coverage_with_risk():
    """阈值扫描必须成对给覆盖率与风险，单给一个就能自欺欺人。"""
    samples = [Sample(0.9, True), Sample(0.8, False), Sample(0.6, False), Sample(0.3, True)]
    sweep = sweep_threshold(samples, current_threshold=0.80)
    current = sweep.at_current_threshold
    assert current["coverage"] == 0.5  # 2/4 自动放过
    assert current["risk"] == 0.5  # 放过的 2 条里错了 1 条
    assert current["escalated"] == 2
    assert sweep.aurc is not None
    assert len(sweep.curve) == 21


def test_reliability_table_bins_are_labeled():
    table = reliability_table([Sample(0.85, True), Sample(0.82, False)])
    assert table[0]["bin"] == "0.8-0.9"
    assert table[0]["count"] == 2
    assert table[0]["accuracy"] == 0.5


# ---------- 自一致性与分位数 ----------


def test_self_consistency_needs_two_runs():
    assert self_consistency([["A"]], min_runs=2)["measured"] is False
    result = self_consistency([["A", "B"], ["A", "B"], ["A"]])
    assert result["measured"] is True
    # A 三次都有、B 两次：稳定率 A=1.0、B=0.667
    assert result["perLabelStability"]["A"] == 1.0
    assert result["perLabelStability"]["B"] == round(2 / 3, 4)


def test_percentiles_track_tail_not_mean():
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    stats = percentiles(values)
    assert stats["p50"] == 3.0
    assert stats["p95"] == 100.0  # 均值会被这条长尾骗，p95 才看得见
    assert percentiles([])["p95"] == 0.0


# ---------- 埋点派生标签（写库口径）----------


def test_locate_method_prefers_explicit_method():
    assert locate_method_name(located=True, method="normalized") == LocateMethod.NORMALIZED.value
    assert locate_method_name(located=True, method="exact") == LocateMethod.EXACT.value
    assert locate_method_name(located=False, method="exact") == LocateMethod.FAILED.value
    # 定位成功但来源不明 → 按最保守的段落兜底记，不给精确匹配的名分
    assert locate_method_name(located=True, method=None) == LocateMethod.SEGMENT_FALLBACK.value
    assert locate_method_name(located=True, method=None, has_evidence=True) == LocateMethod.EVIDENCE.value


def test_stage_timer_accumulates_repeated_stages():
    timer = StageTimer()
    timer.add("retrieval", 1.0)
    timer.add("retrieval", 0.5)
    with timer.stage("llm"):
        pass
    payload = timer.as_dict()
    assert payload["stages"]["retrieval"] == 1.5
    assert payload["accounted"] >= 1.5
    assert payload["total"] >= payload["stages"]["llm"]


def test_token_usage_is_split_by_slot():
    """分槽汇总：主审与复核是两家供应商，混算就算不出每份合同的成本。"""
    records = [
        {"slot": ModelSlot.REVIEW.value, "ok": True, "totalTokens": 100, "promptTokens": 80,
         "completionTokens": 20, "reasoningTokens": 5, "latencySeconds": 1.0},
        {"slot": ModelSlot.JUDGE.value, "ok": False, "totalTokens": 0, "errorType": "LLMError",
         "latencySeconds": 2.5},
    ]
    summary = summarize_token_usage(records)
    assert summary["slots"]["review"]["totalTokens"] == 100
    assert summary["slots"]["judge"]["failures"] == 1
    assert summary["total"]["calls"] == 2
    assert summary["total"]["latencySeconds"] == 3.5


def test_config_fingerprint_carries_judge_identity():
    """指纹必须能说清"复核用的谁、是否独立"，否则历史报告里 glm 自复核与
    deepseek 复核会长得一模一样。"""

    class _Stub:
        llm_provider = "openai_compatible"
        llm_model = "glm-5.3-flash"
        llm_reasoning_effort = "default"
        llm_max_chars_per_call = 12000
        llm_max_tokens = 32768
        judge_enabled = True
        judge_model = "deepseek-flash"
        judge_base_url = "https://api.deepseek.com/v1"
        judge_reasoning_effort = "default"
        judge_same_model_policy = "fail_fast"
        judge_min_confidence = 0.6
        embedding_model = "qwen3-embedding:8b"
        embedding_dimensions = 4096
        rag_top_k = 6
        rag_min_similarity = 0.5
        rag_sparse_enabled = True
        rag_rerank_enabled = True
        rag_rerank_model = "BAAI/bge-reranker-v2-m3"
        rag_rerank_min_score = 0.3
        max_provisions_in_prompt = 14
        rag_use_mock_embedding = False
        confidence_routing_threshold = 0.8
        mandatory_human_review_for_high_risk = True

    fingerprint = config_fingerprint(_Stub())
    assert fingerprint["judge"]["model"] == "deepseek-flash"
    assert fingerprint["judge"]["baseUrl"] == "https://api.deepseek.com/v1"
    assert fingerprint["retrieval"]["embeddingDimensions"] == 4096


# ---------- 金标准夹具解析 ----------


def test_resolve_fixture_walks_up_from_gold_dir(tmp_path):
    """夹具路径要能从金标准目录试到仓库根。

    这里复现一个真实踩过的坑：`fixture` 写的是仓库根相对路径，
    而解析时只退了两级，结果所有用例都"夹具不存在"、评测跑出一个假的 0 分。
    """
    repo = tmp_path / "repo"
    fixture = repo / "testdata" / "contracts" / "POS-01.txt"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("x", encoding="utf-8")
    gold = repo / "testdata" / "gold"
    gold.mkdir()
    resolved = _resolve_fixture("testdata/contracts/POS-01.txt", gold / "POS-01.json")
    assert resolved == fixture


def test_gold_case_reports_label_completeness():
    """覆盖度自检：没标 span/严重度的条目要能被数出来，评测才知道自己没测什么。"""
    case = GoldCase(
        case_id="POS-X",
        fixture=Path("testdata/contracts/POS-X.txt"),
        findings=[
            GoldFinding(rule_code="RATE-001", risk_level="high", span=GoldSpan(1, 20)),
            GoldFinding(rule_code="FEE-002"),
        ],
        must_retrieve=[GoldRef(law_name="中华人民共和国民法典", article_no="第六百七十条")],
    )
    counts = case.labeled_counts
    assert counts["findings"] == 2
    assert counts["withSeverity"] == 1
    assert counts["withSpan"] == 1
    assert counts["mustRetrieve"] == 1
    assert case.expected_rule_codes == {"RATE-001", "FEE-002"}
