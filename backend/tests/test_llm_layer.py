"""LLM 接入层：JSON 解析容错、提示词组装、离线 Mock 与供应商选择。"""

from __future__ import annotations

import json

import pytest
from app.core.config import Settings
from app.models.enums import ClauseCategory
from app.schemas.review import ReviewBatchDraft, ReviewRunRequest
from app.services.llm.base import LLMError
from app.services.llm.factory import build_provider
from app.services.llm.mock import MockLLMProvider
from app.services.llm.openai_compatible import _extract_json_object
from app.services.llm.prompts import PROMPT_VERSION, build_system_prompt, build_user_prompt


def test_extract_plain_json_object():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_wrapped_in_code_fence():
    payload = '```json\n{"findings": []}\n```'
    assert _extract_json_object(payload) == {"findings": []}


def test_extract_json_with_leading_narration():
    payload = '好的，以下是审查结果：\n{"findings": [{"title": "x"}]}\n以上。'
    parsed = _extract_json_object(payload)
    assert parsed["findings"][0]["title"] == "x"


def test_extract_json_rejects_non_object():
    with pytest.raises(LLMError):
        _extract_json_object("[1, 2, 3]")


def test_extract_json_rejects_garbage():
    with pytest.raises(LLMError):
        _extract_json_object("模型今天不想输出 JSON")


def test_system_prompt_has_no_leftover_placeholders():
    request = ReviewRunRequest(perspective="consumer", focus_categories=[ClauseCategory.RATE_AND_FEE])
    prompt = build_system_prompt(
        metric_text="- 综合年化融资成本：36.00%",
        provisions=[],
        rule_hits=["RATE-001 超过 24% 红线"],
        rule_pending=["INFO-001 个人信息共享"],
        request=request,
    )

    for marker in ("<<", ">>", "{_VALID"):
        assert marker not in prompt, f"提示词残留占位符 {marker}"
    assert "rate_and_fee" in prompt
    assert "high | medium | low | none" in prompt
    assert PROMPT_VERSION == "v2"


def test_system_prompt_lists_provision_ids_for_grounding():
    from app.services.rag.retriever import RetrievedProvision

    provision = RetrievedProvision(
        provision_id="abc-123",
        law_name="中华人民共和国民法典",
        article_no="第六百七十条",
        content="借款的利息不得预先在本金中扣除。",
        doc_type="law",
        is_sample=False,
        score=1.0,
        match_kinds=["vector"],
    )
    prompt = build_system_prompt(
        metric_text="",
        provisions=[provision],
        rule_hits=[],
        rule_pending=[],
        request=ReviewRunRequest(),
    )
    # 模型只能引用清单内条目，因此 provisionId 必须原样出现在提示词里
    assert "[#abc-123]" in prompt
    assert "第六百七十条" in prompt


def test_empty_provision_list_forces_low_confidence_instruction():
    prompt = build_system_prompt(
        metric_text="",
        provisions=[],
        rule_hits=[],
        rule_pending=[],
        request=ReviewRunRequest(),
    )
    assert "未检索到可用条文" in prompt


def test_user_prompt_marks_clause_numbers():
    segments = [
        {"index": 1, "title": "第一条 利率", "text": "日利率万分之五"},
        {"index": 2, "title": "第二条 费用", "text": "每月收取 1%"},
    ]
    prompt = build_user_prompt(segments, batch_no=1, batch_total=3)
    assert "[条款 1]" in prompt
    assert "1/3" in prompt


async def test_mock_provider_returns_schema_compatible_payload():
    provider = MockLLMProvider()
    system_prompt = build_system_prompt(
        metric_text="- 综合年化融资成本：36.00%",
        provisions=[],
        rule_hits=[],
        rule_pending=[],
        request=ReviewRunRequest(),
    )
    user_prompt = build_user_prompt(
        [
            {
                "index": 4,
                "title": "第四条 逾期",
                "text": "乙方放弃对甲方催收行为的任何抗辩与异议权利。",
            }
        ],
        batch_no=1,
        batch_total=1,
    )

    result = await provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)
    draft = ReviewBatchDraft.model_validate(result.payload)

    assert draft.findings, "离线 Mock 至少应命中一条霸王条款特征"
    # 启发式命中的霸王条款置信度故意压在阈值下，用来真实验证人工复核链路；
    # 而复述规则引擎年化指标的结论可信度高，不进入队列
    heuristic = [item for item in draft.findings if "启发式" in item.title]
    assert heuristic and all(item.confidence < 0.8 for item in heuristic)
    rate = [item for item in draft.findings if item.confidence >= 0.8]
    assert rate and rate[0].clause_category.value == "rate_and_fee"
    # 清单为空时不得编造引用
    assert all(item.citations == [] for item in draft.findings)
    assert result.provider == "mock"


async def test_mock_provider_only_cites_provisions_from_prompt():
    from app.services.rag.retriever import RetrievedProvision

    provision = RetrievedProvision(
        provision_id="pid-1",
        law_name="银行保险机构消费者权益保护管理办法",
        article_no="第十八条",
        content="示例条文",
        doc_type="department_rule",
        is_sample=False,
        score=1.0,
        match_kinds=["keyword"],
    )
    system_prompt = build_system_prompt(
        metric_text="- 综合年化融资成本：36.00%",
        provisions=[provision],
        rule_hits=[],
        rule_pending=[],
        request=ReviewRunRequest(),
    )
    user_prompt = build_user_prompt(
        [{"index": 1, "title": "第一条", "text": "乙方放弃一切抗辩权利。"}],
        batch_no=1,
        batch_total=1,
    )

    result = await provider_complete(system_prompt, user_prompt)
    draft = ReviewBatchDraft.model_validate(result.payload)
    cited_ids = {citation.provision_id for finding in draft.findings for citation in finding.citations}
    assert cited_ids <= {"pid-1"}, "引用只能来自提示词给出的清单"


async def provider_complete(system_prompt: str, user_prompt: str):
    return await MockLLMProvider().complete_json(
        system_prompt=system_prompt, user_prompt=user_prompt
    )


def test_factory_falls_back_to_mock_without_api_key():
    settings = Settings(llm_api_key="", llm_provider="auto")
    assert build_provider(settings).name == "mock"


def test_factory_uses_openai_compatible_with_api_key():
    settings = Settings(llm_api_key="sk-test", llm_provider="auto", llm_model="deepseek-chat")
    provider = build_provider(settings)
    assert provider.name == "openai_compatible"
    assert provider.model == "deepseek-chat"


def test_factory_respects_explicit_mock_choice():
    settings = Settings(llm_api_key="sk-test", llm_provider="mock")
    assert build_provider(settings).name == "mock"


def test_json_module_roundtrip_helper():
    """确认 camelCase 输出可被 ReviewBatchDraft 直接吃下（模型契约稳定性）。"""
    payload = {
        "batchSummary": "整体判断",
        "findings": [
            {
                "riskLevel": "high",
                "clauseCategory": "rate_and_fee",
                "title": "超过红线",
                "excerpt": "综合年化融资成本 36%",
                "analysis": "超限",
                "suggestion": "降价",
                "confidence": 0.91,
                "citations": [{"provisionId": "p-1", "lawName": "办法", "articleNo": "第十八条"}],
            }
        ],
    }
    draft = ReviewBatchDraft.model_validate(json.loads(json.dumps(payload)))
    assert draft.findings[0].risk_level.value == "high"
    assert draft.findings[0].citations[0].provision_id == "p-1"
