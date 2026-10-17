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


def _reasoning_tokens(usage: Any) -> int:
    """从 OpenAI 兼容响应里取思考 token 数；供应商不返回该字段时得 0。"""
    details = getattr(usage, "completion_tokens_details", None) if usage is not None else None
    return int(getattr(details, "reasoning_tokens", 0) or 0)


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """默认读 `LLM_*` 配置；传入参数用于另一个模型槽位（如复核 judge）。"""
        self._settings = settings
        self._temperature = settings.llm_temperature if temperature is None else temperature
        self._max_tokens = settings.llm_max_tokens if max_tokens is None else max_tokens
        self.model = settings.llm_model if model is None else model
        #: 供审查记录快照：记录本次运行真实生效的思考深度，而不是“当时 .env 里写了什么”
        self.reasoning_effort = reasoning_effort or "default"
        # 思考深度开关只在显式非 default 时下发：部分供应商不认识这个字段，
        # 无条件带上会让整个调用 400
        self._extra_body = (
            {"reasoning_effort": reasoning_effort}
            if reasoning_effort and reasoning_effort != "default"
            else None
        )
        self._client = AsyncOpenAI(
            base_url=base_url or settings.llm_base_url,
            api_key=(api_key if api_key is not None else settings.llm_api_key) or "not-needed",
            timeout=timeout_seconds or settings.llm_timeout_seconds,
        )

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        last_error: Exception | None = None

        for attempt in range(1, 4):
            started = time.perf_counter()
            try:
                completion = await self._client.chat.completions.create(
                    model=self.model,
                    temperature=self._temperature,
                    max_tokens=self._max_tokens,
                    response_format={"type": "json_object"},
                    extra_body=self._extra_body,
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
                choice = completion.choices[0]
                message = choice.message
                content = (message.content or "").strip()
                finish_reason = choice.finish_reason or ""
                usage = completion.usage
                if not content:
                    # 思考型模型最常见的失败不是报错而是“空回”：推理 token 把
                    # max_tokens 预算占完，正文一个字都没输出。不能只写“返回空内容”，
                    # 运维看不出是预算不够还是模型真的无话可说
                    reasoning = getattr(message, "reasoning_content", None) or ""
                    reasoning_tokens = _reasoning_tokens(usage)
                    if finish_reason == "length":
                        raise LLMError(
                            f"模型只输出思考内容就撞到 max_tokens 上限（思考 token={reasoning_tokens}，"
                            f"reasoning {len(reasoning)} 字），正文为空："
                            "请调大 LLM_MAX_TOKENS 或拆小 LLM_MAX_CHARS_PER_CALL"
                        )
                    raise LLMError(
                        f"LLM 返回空内容（finish_reason={finish_reason}, reasoning {len(reasoning)} 字）"
                    )
                latency = time.perf_counter() - started
                return LLMResult(
                    payload=_extract_json_object(content),
                    model=completion.model or self.model,
                    provider=self.name,
                    latency_seconds=round(latency, 3),
                    usage=LLMUsage(
                        prompt_tokens=usage.prompt_tokens if usage else 0,
                        completion_tokens=usage.completion_tokens if usage else 0,
                        total_tokens=usage.total_tokens if usage else 0,
                        reasoning_tokens=_reasoning_tokens(usage),
                    ),
                    raw_content=content,
                    finish_reason=finish_reason,
                )

            if attempt < 3:
                await asyncio.sleep(2 ** (attempt - 1))

        raise LLMError(f"LLM 调用在 3 次重试后仍然失败：{last_error}") from last_error

    async def aclose(self) -> None:
        """关闭 OpenAI 客户端的 HTTP 连接池。

        供应商已经是进程级单例，不关就是每次重建都留下一堆连接：
        健康检查接口曾经每探测一次新建一个 AsyncOpenAI 且不关闭，高并发下先耗完 FD。
        """
        await self._client.close()
