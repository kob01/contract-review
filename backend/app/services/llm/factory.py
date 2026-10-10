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
        return OpenAICompatibleProvider(settings)

    if settings.llm_api_key:
        return OpenAICompatibleProvider(settings)

    logger.warning("未配置 LLM_API_KEY，本次审查使用 Mock 供应商，结果仅用于流程验证")
    return MockLLMProvider()
