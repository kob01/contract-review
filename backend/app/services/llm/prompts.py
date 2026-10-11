"""提示词模板（合规审查 + 强制引用）。

三条不可退让的设计：
1. **只允许引用检索清单内的条文**：把 `provisionId` 清单写进提示词，
   并要求模型原样回填；不在清单内的引用在核验阶段一律判为未核验。
2. **数值由规则引擎算，不由模型算**：把已核算指标作为既定事实注入，
   模型只做定性判断（是否属于加重消费者责任、是否构成搭售等）。
3. **不确定就要降低置信度**：置信度是路由阈值的输入，模型虚报高置信度
   会让该人工复核的结论直接出报告。
"""

from __future__ import annotations

from app.models.enums import ClauseCategory, RiskLevel
from app.schemas.review import ReviewRunRequest
from app.services.rag.retriever import RetrievedProvision

PROMPT_VERSION = "v2"

_VALID_RISK = " | ".join(level.value for level in RiskLevel)
_VALID_CATEGORY = " | ".join(category.value for category in ClauseCategory)

_SYSTEM_TEMPLATE = """你是一名银行消费金融公司的合规审查律师，正在审查一份信贷类合同的合规性。

# 已知事实（由规则引擎确定性核算，不得重新计算或质疑）
<<METRICS>>

# 可引用的法规条文清单（检索增强得到）
每条前的 `[#provisionId]` 是唯一标识，引用时必须原样回填 `provisionId`。
<<PROVISIONS>>

# 规则引擎已定量命中的问题（不要重复报告相同规则，可补充分析）
<<RULE_HITS>>

# 规则引擎无法定量判定、需要你做定性判断的红线
<<RULE_PENDING>>

# 本次审查要求
- 审查立场：<<PERSPECTIVE>>
- 重点条款分类：<<FOCUS>>
- 用户补充要求：<<CUSTOM>>

# 硬性约束
1. 只能依据上面清单内的条文出具合规意见。清单里没有的依据，一律不得引用；
   确有必要引用清单外依据时，把 `confidence` 降到 0.5 以下并在 analysis 中说明缺口。
2. 不得自行推算利率、费用率或期限。需要数值时用"已知事实"里给出的口径。
3. excerpt 必须是所审条款原文中逐字出现的片段（不超过 120 字），不得改写。
4. riskLevel 只能取：<<VALID_RISK>>；clauseCategory 只能取：<<VALID_CATEGORY>>。
5. confidence 表示"该条款确实违规且依据充分"的把握度：
   条文与条款对应关系直接、结论无歧义 → 0.9 以上；
   需要价值衡量（如"不合理加重消费者责任"）→ 0.6 至 0.8；
   依据不足或条款含糊 → 0.5 以下，此类结论会自动流转人工法务复核。
6. 没有问题的条款不要报告。全部无问题时 findings 返回空数组。
7. 严格只输出一个 JSON 对象，不要输出解释文字，不要使用 Markdown 代码块。

# 输出结构（字段名必须完全一致）
{
  "batchSummary": "本批次条款的整体判断，一两句话",
  "findings": [
    {
      "riskLevel": "high",
      "clauseCategory": "rate_and_fee",
      "title": "不超过 20 字的合规问题摘要",
      "excerpt": "合同原文逐字片段",
      "segmentIndex": 3,
      "analysis": "为什么违反监管要求或构成霸王条款，需引用清单内条文",
      "suggestion": "具体到改哪句、怎么改",
      "rewrittenText": "建议改写后的完整条款表述，无法给出时为 null",
      "confidence": 0.85,
      "citations": [
        {"provisionId": "清单中给出的 id", "lawName": "…", "articleNo": "…", "quote": "条文原文片段"}
      ],
      "ruleCode": "若对应上面列出的红线，回填其编码，否则为 null"
    }
  ]
}
"""

_PERSPECTIVE_HINTS = {
    "consumer": "站在金融消费者立场，重点识别过高成本、免责与放弃权利、搭售与不当催收。",
    "institution": "站在放款机构合规立场，重点识别可能引发监管处罚与格式条款无效的条款。",
    "neutral": "以中立合规视角审查，同时指出双方权利义务失衡与监管明令禁止的安排。",
}


def build_system_prompt(
    *,
    metric_text: str,
    provisions: list[RetrievedProvision],
    rule_hits: list[str],
    rule_pending: list[str],
    request: ReviewRunRequest,
) -> str:
    rendered_provisions = (
        "\n".join(
            f"- [#{item.provision_id}] 《{item.law_name}》{item.article_no}"
            f"{'（仅为占位登记，原文未导入，不得作为结论依据）' if item.is_sample else ''}："
            f"{item.content}"
            for item in provisions
        )
        or "（本轮未检索到可用条文，任何合规结论都必须把 confidence 降到 0.5 以下）"
    )

    focus = (
        "、".join(category.value for category in request.focus_categories)
        if request.focus_categories
        else "全部条款"
    )
    custom = request.custom_instructions.strip() if request.custom_instructions else "无"

    return (
        _SYSTEM_TEMPLATE.replace("<<METRICS>>", metric_text or "（未抽取到可量化指标）")
        .replace("<<PROVISIONS>>", rendered_provisions)
        .replace(
            "<<RULE_HITS>>",
            "\n".join(f"- {line}" for line in rule_hits) or "- （无）",
        )
        .replace(
            "<<RULE_PENDING>>",
            "\n".join(f"- {line}" for line in rule_pending) or "- （无）",
        )
        .replace(
            "<<PERSPECTIVE>>",
            _PERSPECTIVE_HINTS.get(request.perspective, _PERSPECTIVE_HINTS["neutral"]),
        )
        .replace("<<FOCUS>>", focus)
        .replace("<<CUSTOM>>", custom)
        .replace("<<VALID_RISK>>", _VALID_RISK)
        .replace("<<VALID_CATEGORY>>", _VALID_CATEGORY)
    )


def build_user_prompt(segments: list[dict], *, batch_no: int, batch_total: int) -> str:
    lines = [
        f"以下是合同的第 {batch_no}/{batch_total} 批条款原文，请按要求输出审查结果 JSON。",
        "",
    ]
    for segment in segments:
        lines.append(f"[条款 {segment.get('index')}] {(segment.get('title') or '（无标题）').strip()}")
        lines.append((segment.get("text") or "").strip())
        lines.append("")
    return "\n".join(lines)


def describe_rule_hits_for_prompt(hits) -> list[str]:
    """把规则命中转成提示词里的自然语言条目。"""
    return [
        f"{hit.rule_code} {hit.rule_name}：{hit.analysis}（阈值口径：{hit.threshold_desc or hit.rule_name}）"
        for hit in hits
    ]


def describe_pending_rules(rules, metrics_text: str) -> list[str]:
    """规则无法定量判定时的提示条目，`metrics_text` 用于说明缺哪个数。"""
    del metrics_text  # 指标已在 <<METRICS>> 区块整体给出，此处只列规则本身
    return [
        f"{rule.code} {rule.name}：{rule.guidance_text} 建议：{rule.suggestion_text}"
        for rule in rules
    ]


#: 复核提示词版本，与主审查提示词分开计量，便于对比“改词后误报率是否变化”
JUDGE_PROMPT_VERSION = "judge-v1"

_JUDGE_SYSTEM_TEMPLATE = """你是独立复核律师（第二意见模型）。你的职责是质疑已给出的合规结论，而不是附和它。
审查结论已经过了规则引擎与引用核验，你只需要回答：“这条结论站不站得住”。

# 可引用的法规条文清单（与主审查看到的是同一份）
<<PROVISIONS>>

# 判定口径
- agree：该结论是否成立（条款确实存在所述问题，且定性正确）
- citationOk：所引条文能否支撑结论（引用不支撑 = false，即使结论方向对）
- suggestedRiskLevel：你认为应有的风险等级，与原判定一致时也要回填
- confidence：你对本次裁定的把握度（0~1）

# 硬性约束
1. 原文片段与结论不搭、把程序/管辖条款说成实体违规、结论靠猜测补充事实 → agree=false。
2. 不得因为分析写得长、语气确定就默认成立；也不得因为等级偏保守就判不成立。
3. 拿不准时给低 confidence 让它转人工，不要附和可疑结论——漏放比误报严重。
4. 每条都要给 verdicts 条目，index 必须原样回填。
5. riskLevel 只能取：<<VALID_RISK>>。
6. 严格只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码块。

# 输出结构（字段名必须完全一致）
{
  "verdicts": [
    {
      "index": 0,
      "agree": true,
      "citationOk": true,
      "suggestedRiskLevel": "high",
      "confidence": 0.8,
      "reason": "不超过 40 字的判定理由"
    }
  ]
}
"""


def build_judge_system_prompt(provisions: list[RetrievedProvision]) -> str:
    rendered = (
        "\n".join(
            f"- [#{item.provision_id}] 《{item.law_name}》{item.article_no}"
            f"{'（仅为占位登记，原文未导入）' if item.is_sample else ''}：{item.content}"
            for item in provisions
        )
        or "（本轮没有可用条文，凡给出条文依据的结论都应判 citationOk=false）"
    )
    return _JUDGE_SYSTEM_TEMPLATE.replace("<<PROVISIONS>>", rendered).replace(
        "<<VALID_RISK>>", _VALID_RISK
    )


def build_judge_user_prompt(cases: list[dict], *, batch_no: int, batch_total: int) -> str:
    """`cases` 至少含 index/title/excerpt/analysis/riskLevel/source/confidence/citations。"""
    lines = [
        f"以下是待复核的合规结论（第 {batch_no}/{batch_total} 批），逐条给出裁定 JSON。",
        "",
    ]
    for case in cases:
        header = (
            f"[结论 {case['index']}] {case.get('title') or ''}"
            f"（等级 {case.get('riskLevel')}，来源 {case.get('source')}）"
        )
        lines.append(header)
        lines.append(f"原文片段：{(case.get('excerpt') or '').strip()}")
        lines.append(f"分析：{(case.get('analysis') or '').strip()}")
        lines.append(f"建议：{(case.get('suggestion') or '').strip()}")
        citations = case.get("citations") or []
        if citations:
            rendered = "; ".join(
                f"《{item.get('lawName') or ''}》{item.get('articleNo') or ''}"
                f"{'[未核验]' if not item.get('verified') else ''}"
                for item in citations
            )
            lines.append(f"引用：{rendered}")
        lines.append("")
    return "\n".join(lines)
