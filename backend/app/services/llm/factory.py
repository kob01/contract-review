"""供应商选择。

`LLM_PROVIDER=auto`（默认）时：配置了 `LLM_API_KEY` 走 OpenAI 兼容接口，
否则退回 Mock 供应商并在日志中明确提示，避免"看起来在工作、其实在胡说"。
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.llm.base import LLMProvider
from app.services.llm.mock import MockLLMProvider
from app.services.llm.openai_compatible import OpenAICompatibleProvider

logger = get_logger(__name__)


def build_provider(settings: Settings) -> LLMProvider:
    choice = settings.llm_provider

    if choice == "mock":
        return MockLLMProvider()

    if choice == "openai_compatible":
        if not settings.llm_api_key:
            logger.warning("已指定 openai_compatible 但 LLM_API_KEY 为空，调用很可能被拒绝")
        return OpenAICompatibleProvider(
            settings, reasoning_effort=settings.llm_reasoning_effort
        )

    if settings.llm_api_key:
        return OpenAICompatibleProvider(settings, reasoning_effort=settings.llm_reasoning_effort)

    logger.warning("未配置 LLM_API_KEY，本次审查使用 Mock 供应商，结果仅用于流程验证")
    return MockLLMProvider()


def build_judge_provider(settings: Settings) -> LLMProvider | None:
    """构造结论复核（judge）模型，与主审查模型分属不同配置槽位。

    返回 None 表示本次不执行复核（开关关闭、无密钥、或主链路还在 Mock）：
    复核是额外一道安全网，它的缺席不能反过来阻断主链路出报告，
    但必须写进 `ContractReview.judge_model` 为空这个事实，由报告页说明“未复核”。
    """
    if not settings.judge_enabled:
        logger.info("结论复核已由 JUDGE_ENABLED 关闭")
        return None

    api_key = settings.judge_api_key or settings.llm_api_key
    if not api_key:
        logger.warning("未配置复核密钥（JUDGE_API_KEY/LLM_API_KEY），本次不执行结论复核")
        return None

    if isinstance(build_provider(settings), MockLLMProvider):
        # Mock 主模型输出的是样例结论，拿它做复核只会得到一个自证循环的“通过”
        logger.warning("主审查链路仍为 Mock，跳过结论复核")
        return None

    return OpenAICompatibleProvider(
        settings,
        base_url=settings.judge_base_url or settings.llm_base_url,
        api_key=api_key,
        model=settings.judge_model,
        temperature=settings.judge_temperature,
        max_tokens=settings.judge_max_tokens,
        timeout_seconds=settings.judge_timeout_seconds,
        reasoning_effort=settings.judge_reasoning_effort,
    )
