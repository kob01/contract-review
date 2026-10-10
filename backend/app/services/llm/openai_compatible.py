"""OpenAI 兼容接口实现。

覆盖 OpenAI、DeepSeek、通义千问（compatible-mode）、Kimi、vLLM、Ollama(/v1) 等
只需改 `LLM_BASE_URL` 与 `LLM_MODEL` 即可切换的供应商。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.llm.base import LLMError, LLMResult, LLMUsage

logger = get_logger(__name__)

#: 模型偶尔会把 JSON 包在 ```json ... ``` 里，需要先剥壳
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)


def _extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group("body")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 退一步：截取第一个 { 到最后一个 } 之间的内容
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"模型返回内容不是 JSON 对象：{text[:200]!r}") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:  # pragma: no cover - 依赖模型异常输出
            raise LLMError(f"解析模型 JSON 失败：{exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMError(f"期望 JSON 对象，实际得到 {type(parsed).__name__}")
    return parsed


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model = settings.llm_model
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key or "not-needed",
            timeout=settings.llm_timeout_seconds,
        )

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        last_error: Exception | None = None

        for attempt in range(1, 4):
            started = time.perf_counter()
            try:
                completion = await self._client.chat.completions.create(
                    model=self.model,
                    temperature=self._settings.llm_temperature,
                    max_tokens=self._settings.llm_max_tokens,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
            except (APITimeoutError, APIConnectionError) as exc:
                last_error = exc
                logger.warning("LLM 网络异常（第 %s 次）：%s", attempt, exc)
            except APIStatusError as exc:
                # 4xx 属请求问题，重试无意义
                if exc.status_code < 500:
                    raise LLMError(f"LLM 请求被拒绝（{exc.status_code}）：{exc}") from exc
                last_error = exc
                logger.warning("LLM 服务端错误（第 %s 次）：%s", attempt, exc.status_code)
            else:
                content = (completion.choices[0].message.content or "").strip()
                if not content:
                    raise LLMError("LLM 返回空内容")
                latency = time.perf_counter() - started
                usage = completion.usage
                return LLMResult(
                    payload=_extract_json_object(content),
                    model=completion.model or self.model,
                    provider=self.name,
                    latency_seconds=round(latency, 3),
                    usage=LLMUsage(
                        prompt_tokens=usage.prompt_tokens if usage else 0,
                        completion_tokens=usage.completion_tokens if usage else 0,
                        total_tokens=usage.total_tokens if usage else 0,
                    ),
                    raw_content=content,
                )

            if attempt < 3:
                await asyncio.sleep(2 ** (attempt - 1))

        raise LLMError(f"LLM 调用在 3 次重试后仍然失败：{last_error}") from last_error
