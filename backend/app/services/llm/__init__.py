"""LLM 接入层。"""

from app.services.llm.base import LLMError, LLMProvider, LLMResult, LLMUsage
from app.services.llm.factory import build_provider
from app.services.llm.mock import MockLLMProvider
from app.services.llm.openai_compatible import OpenAICompatibleProvider

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResult",
    "LLMUsage",
    "MockLLMProvider",
    "OpenAICompatibleProvider",
    "build_provider",
]
