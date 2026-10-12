"""LLM 供应商抽象。

上层审查引擎只依赖 `LLMProvider` 协议，更换供应商不影响业务逻辑。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResult(BaseModel):
    """一次补全调用的结果。"""

    payload: dict[str, Any] = Field(default_factory=dict)
    model: str = ""
    provider: str = ""
    latency_seconds: float = 0.0
    usage: LLMUsage = Field(default_factory=LLMUsage)
    raw_content: str = ""


class LLMError(RuntimeError):
    """模型调用失败，或返回内容无法解析为约定结构。"""


class LLMProvider(Protocol):
    """要求返回 JSON 对象的补全接口。"""

    name: str
    model: str
    #: 本次调用实际使用的思考深度（`default` = 不下发该参数，沿用供应商默认）。
    #: 审查记录需要把它存成快照：思考深度是配置，两次运行可能不同，不存就无法复盘
    reasoning_effort: str

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        """以 JSON 模式补全，返回已解析的字典。

        实现必须在无法得到合法 JSON 时抛出 `LLMError`，而不是静默返回空对象，
        否则上层会把"模型故障"误判成"合同无风险"。
        """
        ...

    async def aclose(self) -> None:
        """释放底层 HTTP 连接。

        供应商对象现在是进程级单例，单例就必须能被关掉：
        否则健康检查与任务重提会把连接池越积越多。
        """
        ...
