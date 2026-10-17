"""复核通道的独立性（回音壁防护）与客户端复用。

主审与复核来路不同时，复核才是一个独立信号；同一家同一个模型只是把同一份
认知偏差再表述一遍。这里测的是"配置怎么说、系统就怎么做"：
warn / rule_check / fail_fast 三种口径必须各自成立，且不能为了读配置而新建客户端。
"""

from __future__ import annotations

import pytest
from app.core.config import Settings
from app.services.llm.factory import (
    JUDGE_MODE_DISABLED,
    JUDGE_MODE_LLM,
    JUDGE_MODE_RULE_CHECK,
    RULE_CHECK_MODEL,
    LLMConfigError,
    build_judge_provider,
    describe_judge_backend,
    describe_llm_backend,
    main_slot_will_mock,
    shared_provider,
    validate_model_slots,
)

GLM = "glm-5.3-flash"


def settings_with(**overrides) -> Settings:
    base = {
        "llm_api_key": "sk-test",
        "llm_provider": "openai_compatible",
        "llm_model": GLM,
        "judge_model": GLM,
        "judge_base_url": "",
        "judge_api_key": "",
        # 显式写死而不是继承 .env：仓库默认口径已改成 fail_fast（复核必须独立来路），
        # 而这组夹具要测的是"同来路但仍要模型在回路里"的 warn 形态
        "judge_same_model_policy": "warn",
        "queue_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


def test_same_model_slot_is_reported_as_not_independent():
    """默认口径 warn：照旧调用复核模型，但必须承认它不是独立信号。"""
    slot = describe_judge_backend(settings_with(judge_same_model_policy="warn"))
    assert slot.mode == JUDGE_MODE_LLM
    assert slot.model == GLM
    assert slot.independent is False
    assert "不是独立信号" in slot.warning


DEEPSEEK = "deepseek-flash"


def test_distinct_model_slot_is_independent():
    """两家来路不同且各自有密钥：这才是独立信号，也不该再报回音壁。"""
    slot = describe_judge_backend(
        settings_with(
            judge_model=DEEPSEEK,
            judge_base_url="https://api.deepseek.com/v1",
            judge_api_key="sk-deepseek",
        )
    )
    assert slot.independent is True
    assert slot.mode == JUDGE_MODE_LLM
    assert slot.warning == ""


def test_foreign_endpoint_without_own_key_disables_judge_instead_of_reusing_key():
    """跨供应商复核却没那家的密钥时，绝不把主审密钥发过去。

    复用会换来成批 401，而 401 在报告里长成"复核模型很差"，
    把配置事故伪装成模型质量问题。宁可显式不复核（结论标未复核、路由照常）。
    """
    slot = describe_judge_backend(
        settings_with(judge_model=DEEPSEEK, judge_base_url="https://api.deepseek.com/v1")
    )
    assert slot.mode == JUDGE_MODE_DISABLED
    assert slot.provider is None
    assert "JUDGE_API_KEY" in slot.warning
    # 同一条口径也要出现在 /health 的配置告警里，否则运维只看健康检查会以为复核在跑
    drifted = settings_with(judge_model=DEEPSEEK, judge_base_url="https://api.deepseek.com/v1")
    assert any("JUDGE_API_KEY" in item for item in drifted.audit_warnings())


def test_rule_check_policy_downgrades_to_deterministic_judge():
    """同模型时改走确定性复核：不叫模型，裁定全部可复现。"""
    slot = describe_judge_backend(settings_with(judge_same_model_policy="rule_check"))
    assert slot.mode == JUDGE_MODE_RULE_CHECK
    assert slot.provider is None
    assert slot.model == RULE_CHECK_MODEL


def test_fail_fast_policy_raises_at_configuration_time():
    with pytest.raises(LLMConfigError):
        describe_judge_backend(settings_with(judge_same_model_policy="fail_fast"))

    with pytest.raises(LLMConfigError):
        validate_model_slots(settings_with(judge_same_model_policy="fail_fast"))


def test_disabled_or_keyless_judge_reports_disabled_not_silence():
    disabled = describe_judge_backend(settings_with(judge_enabled=False))
    assert disabled.mode == JUDGE_MODE_DISABLED
    assert disabled.model == ""

    keyless = describe_judge_backend(settings_with(judge_api_key="", llm_api_key=""))
    assert keyless.mode == JUDGE_MODE_DISABLED


def test_mock_main_channel_skips_judge():
    """主链路是 Mock 时不复核：拿样例结论去复核只是自证循环。"""
    settings = settings_with(llm_provider="mock", llm_api_key="")
    assert main_slot_will_mock(settings) is True
    assert describe_judge_backend(settings).mode == JUDGE_MODE_DISABLED
    assert build_judge_provider(settings) is None


def test_describe_functions_build_no_clients():
    """/health 每秒可能被探测多次，读描述不能顺手建连接池。"""
    settings = settings_with()
    first = describe_llm_backend(settings)
    second = describe_llm_backend(settings)
    assert first == second
    assert first[0] == "openai_compatible"

    describe_judge_backend(settings)
    describe_judge_backend(settings)
    # 共享供应商按配置指纹复用：同一份配置两次取到的是同一个对象
    assert shared_provider(settings) is shared_provider(settings)


def test_shared_provider_rebuilt_when_model_changes():
    a = settings_with(llm_model=GLM, queue_enabled=False)
    b = settings_with(llm_model="deepseek-chat", queue_enabled=False)
    assert shared_provider(a) is not shared_provider(b)
