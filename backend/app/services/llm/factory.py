"""供应商选择与进程级复用。

三件事在这里收口：

1. **同一个进程只建一份客户端**。`AsyncOpenAI` 自带连接池，按请求新建会把
   连接数拆散并在健康检查里持续泄露（每次探测多一个不关闭的客户端），
   因此对外一律走 `shared_provider` / `shared_judge_provider`。
2. **健康检查不建客户端**。`describe_*` 系列是纯函数，只看配置就能说出
   "将用哪个模型、哪一档思考、复核是否独立"，探测不再产生网络对象。
3. **主审与复核必须说清是否同源**。同一个模型给自己盖章只是附和（回音壁），
   由 `JUDGE_SAME_MODEL_POLICY` 决定 warn / rule_check / fail_fast 三种口径。

`LLM_PROVIDER=auto` 时：配置了 `LLM_API_KEY` 走 OpenAI 兼容接口，
否则退回 Mock 供应商并在日志中明确提示，避免"看起来在工作、其实在胡说"。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.core.config import Settings
from app.core.logging import get_logger
from app.services.llm.base import LLMProvider
from app.services.llm.mock import MockLLMProvider
from app.services.llm.openai_compatible import OpenAICompatibleProvider

logger = get_logger(__name__)

#: 复核槽位的实际形态，写进审查记录与 /health，不能只留一个模型名
JUDGE_MODE_LLM = "llm"
JUDGE_MODE_RULE_CHECK = "rule_check"
JUDGE_MODE_DISABLED = "disabled"

#: 确定性规则复核写进 `ContractReview.judge_model` 的标识：它不是模型名，
#: 报告页必须能区分“另一家的模型复核过”与“按硬信号复核过”
RULE_CHECK_MODEL = "rule-check-v1"


class LLMConfigError(RuntimeError):
    """配置层面就不成立的模型组合（`JUDGE_SAME_MODEL_POLICY=fail_fast` 时抛出）。"""


@dataclass(slots=True, frozen=True)
class JudgeSlot:
    """复核通道的装配结果。

    `provider=None` 表示本次不调复核模型，`mode` 说明为什么不调：
    复核是额外一道安全网，它的缺席不能反过来阻断主链路出报告，
    但必须写进 `ContractReview.judge_model` 与报告说明，由界面告知"未复核"。
    """

    provider: LLMProvider | None = None
    mode: str = JUDGE_MODE_DISABLED
    model: str = ""
    #: 主审与复核是否来路不同；False 时复核结论只是同一模型的第二次表述
    independent: bool = False
    warning: str = ""


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


def main_slot_will_mock(settings: Settings) -> bool:
    """不建客户端地判断主审通道是否会走 Mock（供复核装配复用）。

    复核装配原本靠 `isinstance(build_provider(settings), MockLLMProvider)`，
    那是为了问一句话而新建一整个连接池。
    """
    if settings.llm_provider == "mock":
        return True
    return settings.llm_provider == "auto" and not settings.llm_api_key


def describe_llm_backend(settings: Settings) -> tuple[str, str, str]:
    """返回 (provider 名, 模型, 思考深度)，只读配置不产生网络连接。"""
    if main_slot_will_mock(settings):
        provider = MockLLMProvider()
        return provider.name, provider.model, provider.reasoning_effort
    return OpenAICompatibleProvider.name, settings.llm_model, settings.llm_reasoning_effort


def describe_judge_backend(settings: Settings) -> JudgeSlot:
    """只读配置描述复核槽位的最终形态，与 `build_judge_slot` 同口径。

    /health 与规则页都要展示"复核是否独立"，但探测接口绝不能为此建客户端。
    """
    if not settings.judge_enabled:
        return JudgeSlot(mode=JUDGE_MODE_DISABLED, warning="JUDGE_ENABLED=false")

    if not (settings.judge_api_key or settings.llm_api_key):
        return JudgeSlot(mode=JUDGE_MODE_DISABLED, warning="未配置复核密钥，本次不执行结论复核")

    #: 跨供应商复核却没有那家的密钥：不能拿主审密钥去撞别家端点（必然成批 401，
    #: 且会把"复核失败"伪装成"模型质量差"）。宁可显式不复核，也不制造假失败
    if settings.judge_missing_own_key:
        return JudgeSlot(
            mode=JUDGE_MODE_DISABLED,
            warning=(
                f"复核端点 {settings.judge_base_url} 与主审不同但未配置 JUDGE_API_KEY，"
                "已跳过结论复核（不得复用主审密钥）"
            ),
        )

    if main_slot_will_mock(settings):
        return JudgeSlot(
            mode=JUDGE_MODE_DISABLED,
            warning="主审查链路仍为 Mock，跳过结论复核（Mock 样例结论没有复核价值）",
        )

    independent = not settings.judge_shares_main_slot
    if independent:
        return JudgeSlot(
            mode=JUDGE_MODE_LLM,
            model=settings.judge_model,
            independent=True,
        )

    # 同来路：按口径决定照旧调用、退回确定性规则复核，还是直接报错
    if settings.judge_same_model_policy == "fail_fast":
        raise LLMConfigError(
            f"复核模型与主审同为 {settings.judge_model} 且端点相同，"
            "JUDGE_SAME_MODEL_POLICY=fail_fast 要求两个槽位来路不同："
            "请给 JUDGE_BASE_URL/JUDGE_MODEL 配另一个基座，或把口径改成 rule_check"
        )
    if settings.judge_same_model_policy == "rule_check":
        return JudgeSlot(
            mode=JUDGE_MODE_RULE_CHECK,
            model=RULE_CHECK_MODEL,
            warning="主审与复核同模型，已降级为确定性规则复核（引用核验 + 双通道一致性裁定）",
        )
    return JudgeSlot(
        mode=JUDGE_MODE_LLM,
        model=settings.judge_model,
        independent=False,
        warning=f"复核模型与主审同为 {settings.judge_model}，不是独立信号",
    )


def build_judge_slot(settings: Settings) -> JudgeSlot:
    """按配置装配复核通道，并把"为什么是这个结果"一起返回。"""
    descriptor = describe_judge_backend(settings)

    if descriptor.mode != JUDGE_MODE_LLM:
        if descriptor.warning:
            (logger.warning if descriptor.mode == JUDGE_MODE_RULE_CHECK else logger.info)(
                "%s", descriptor.warning
            )
        return descriptor

    provider = _new_judge_provider(settings)
    if descriptor.warning:
        logger.warning("%s", descriptor.warning)
    return replace(descriptor, provider=provider, model=provider.model)


def _new_judge_provider(settings: Settings) -> LLMProvider:
    return OpenAICompatibleProvider(
        settings,
        base_url=settings.judge_base_url or settings.llm_base_url,
        api_key=settings.judge_api_key or settings.llm_api_key,
        model=settings.judge_model,
        temperature=settings.judge_temperature,
        max_tokens=settings.judge_max_tokens,
        timeout_seconds=settings.judge_timeout_seconds,
        reasoning_effort=settings.judge_reasoning_effort,
    )


def build_judge_provider(settings: Settings) -> LLMProvider | None:
    """构造结论复核（judge）模型；只需供应商对象的调用方用这个入口。"""
    return build_judge_slot(settings).provider


def validate_model_slots(settings: Settings) -> list[str]:
    """启动期校验模型槽位，返回应当记录的告警列表。

    `fail_fast` 口径下 `describe_judge_backend` 会直接抛 `LLMConfigError`，
    让服务在启动阶段就把"回音壁配置"暴露出来，而不是运行中悄悄自我附和。
    """
    slot = describe_judge_backend(settings)
    if slot.warning:
        logger.warning("模型槽位：%s", slot.warning)
    return [slot.warning] if slot.warning else []


# ---------- 进程级单例 ----------

_shared: dict[str, tuple[str, LLMProvider]] = {}


def _shared_for(kind: str, settings: Settings, factory) -> LLMProvider:
    """按配置指纹复用客户端；配置变了才重建（便于改档位与测试）。"""
    fingerprint = _fingerprint(settings, kind)
    cached = _shared.get(kind)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    provider = factory(settings)
    _shared[kind] = (fingerprint, provider)
    return provider


def _fingerprint(settings: Settings, kind: str) -> str:
    if kind == "judge":
        return (
            f"judge::{settings.judge_base_url or settings.llm_base_url}::{settings.judge_model}"
            f"::{settings.judge_temperature}::{settings.judge_max_tokens}"
            f"::{settings.judge_timeout_seconds}::{settings.judge_reasoning_effort}"
        )
    return (
        f"llm::{settings.llm_provider}::{settings.llm_base_url}::{settings.llm_model}"
        f"::{settings.llm_temperature}::{settings.llm_max_tokens}"
        f"::{settings.llm_timeout_seconds}::{settings.llm_reasoning_effort}"
    )


def shared_provider(settings: Settings) -> LLMProvider:
    """主审通道的进程级供应商。"""
    return _shared_for("llm", settings, build_provider)


def shared_judge_slot(settings: Settings) -> JudgeSlot:
    """复核通道的进程级装配结果（含未调用模型的情形）。

    先读配置描述，只在没命中缓存时才新建 provider：
    否则“为了复用而装配”反而每调一次多建一个客户端，止不住泄露。
    """
    descriptor = describe_judge_backend(settings)
    if descriptor.mode != JUDGE_MODE_LLM:
        return descriptor
    provider = _shared_for("judge", settings, _new_judge_provider)
    return replace(descriptor, provider=provider, model=provider.model)


async def aclose_shared_providers() -> None:
    """应用/worker 退出时释放模型侧连接。"""
    for kind, (_, provider) in list(_shared.items()):
        _shared.pop(kind, None)
        try:
            await provider.aclose()
        except Exception as exc:  # pragma: no cover - 关闭失败不影响退出
            logger.warning("关闭 %s 供应商连接失败：%s", kind, exc)
