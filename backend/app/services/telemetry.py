"""评测埋点的公共件：阶段计时、结论级核验标签、降级留痕与 token 汇总。

放在服务层而不是模型层，是因为这些是"算出来的观测值"，不是持久化结构本身。
全部为纯函数或无网络依赖，`scripts/eval.py` 与审查写路径共用同一套口径——
评测脚本再实现一遍标签判定，是"运行时与评测口径不一致"最常见的来源。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.models.enums import CitationVerification, CitationVerifyCode, LocateMethod


class StageTimer:
    """按阶段累计墙钟时间。

    只存一个 `elapsed_seconds` 就无法回答"这份合同慢在检索还是慢在模型"，
    而阶段耗时是 ORCH-07 / PAR-14 / RET-14 / GEN-16 这些指标的唯一数据源。
    同名阶段多次进入会累加（逐批检索、逐批模型调用都属同类阶段）。
    """

    def __init__(self) -> None:
        self._stages: dict[str, float] = {}
        self._started = time.perf_counter()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self._stages[name] = self._stages.get(name, 0.0) + (time.perf_counter() - started)

    def add(self, name: str, seconds: float) -> None:
        """记录已经测好的耗时：并发批次里不便套上下文管理器的场合。"""
        self._stages[name] = self._stages.get(name, 0.0) + max(0.0, seconds)

    def as_dict(self) -> dict[str, Any]:
        total = time.perf_counter() - self._started
        stages = {name: round(seconds, 3) for name, seconds in self._stages.items()}
        return {
            "total": round(total, 3),
            "stages": stages,
            # 阶段和与总墙钟的差 = 并发重叠 + 未计时缝隙；差得越多说明埋点越不全
            "accounted": round(sum(stages.values()), 3),
        }


@dataclass(slots=True)
class DegradationEvent:
    """一次降级：能继续出结果，但结果的可信范围变了。

    管道里散落着十几处降级（检索通道挂了、某批模型输出无效、定位退回段落级…）。
    以前只有一行 `logger.warning`，事后无法统计"这次审查降了几级、降在哪"，
    而降级留痕率（ORCH-06）是所有其它指标可信的前提。
    """

    code: str
    detail: str = ""
    stage: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail[:300], "stage": self.stage}


@dataclass(slots=True)
class DegradationLog:
    """一次审查内的降级收集器。"""

    events: list[DegradationEvent] = field(default_factory=list)

    def add(self, code: str, detail: str = "", *, stage: str = "") -> None:
        self.events.append(DegradationEvent(code=code, detail=detail, stage=stage))

    def codes(self) -> list[str]:
        return [event.code for event in self.events]

    def to_list(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]


def citation_verification_status(citations: list[dict] | None) -> str:
    """把一条结论的引用集合归成一个可聚合的状态标签（GEN-06）。

    口径与 `ReviewFinding.has_unverified_citation` 一致但更细：
    "全占位"与"库里根本没有"必须分开统计——前者是知识库欠账（RET-08），
    后者是模型幻觉（GEN-07），两者的整改动作完全不同。
    """
    if not citations:
        return CitationVerification.NONE.value

    verified = placeholder = missing = 0
    for citation in citations:
        code = citation.get("verifyCode")
        if code is None:
            # 历史数据没有 verifyCode：按布尔字段兜底，宁可归到"未收录"也别假装核验过
            code = (
                CitationVerifyCode.OK.value
                if citation.get("verified", False)
                else CitationVerifyCode.NOT_IN_KB.value
            )
        if code == CitationVerifyCode.OK.value:
            verified += 1
        elif code == CitationVerifyCode.PLACEHOLDER.value:
            placeholder += 1
        else:
            missing += 1

    if verified == len(citations):
        return CitationVerification.VERIFIED.value
    if verified:
        return CitationVerification.PARTIAL.value
    if placeholder and not missing:
        return CitationVerification.PLACEHOLDER.value
    return CitationVerification.NOT_IN_KB.value


def locate_method_name(
    *, located: bool, method: str | None, has_evidence: bool = False
) -> str:
    """把定位结果归一成 `LocateMethod` 取值（GEN-12 的分母口径）。

    规则通道的偏移来自指标抽取自带证据（`evidence`），与模型 excerpt 回连是两条
    不同的质量链路；混成一个"定位成功"就没法分别度量，也没法发现
    "靠去空白模糊匹配定位上"的结论其实错位风险更高。
    """
    if not located:
        return LocateMethod.FAILED.value
    if method == "normalized":
        return LocateMethod.NORMALIZED.value
    if method == "segment_fallback":
        return LocateMethod.SEGMENT_FALLBACK.value
    if method == "exact":
        return LocateMethod.EXACT.value
    if has_evidence:
        return LocateMethod.EVIDENCE.value
    # 定位成功但来源不明：按最保守的"段落兜底"记，不给它精确匹配的名分
    return LocateMethod.SEGMENT_FALLBACK.value


def provision_ids(items: list[Any]) -> list[str]:
    """从 `RetrievedProvision` 列表取 id：本批模型到底看了哪些条文（GEN-08 分母）。"""
    return [str(getattr(item, "provision_id", "") or "") for item in items]


def summarize_token_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐次调用记录汇总成审查级 token 账（GEN-16、BIZ-05）。

    分槽位汇总：复核通道用的是独立模型（DeepSeek），主审是 GLM，
    两家单价与限速都不同，混成一个总数就算不出每份合同的真实成本。
    """
    slots: dict[str, dict[str, Any]] = {}
    for record in records:
        slot = str(record.get("slot") or "review")
        bucket = slots.setdefault(
            slot,
            {
                "calls": 0,
                "failures": 0,
                "promptTokens": 0,
                "completionTokens": 0,
                "totalTokens": 0,
                "reasoningTokens": 0,
                "latencySeconds": 0.0,
            },
        )
        bucket["calls"] += 1
        if not record.get("ok", True):
            bucket["failures"] += 1
        bucket["promptTokens"] += int(record.get("promptTokens") or 0)
        bucket["completionTokens"] += int(record.get("completionTokens") or 0)
        bucket["totalTokens"] += int(record.get("totalTokens") or 0)
        bucket["reasoningTokens"] += int(record.get("reasoningTokens") or 0)
        bucket["latencySeconds"] = round(
            bucket["latencySeconds"] + float(record.get("latencySeconds") or 0.0), 3
        )

    total = {
        "calls": sum(bucket["calls"] for bucket in slots.values()),
        "failures": sum(bucket["failures"] for bucket in slots.values()),
        "promptTokens": sum(bucket["promptTokens"] for bucket in slots.values()),
        "completionTokens": sum(bucket["completionTokens"] for bucket in slots.values()),
        "totalTokens": sum(bucket["totalTokens"] for bucket in slots.values()),
        "reasoningTokens": sum(bucket["reasoningTokens"] for bucket in slots.values()),
        "latencySeconds": round(
            sum(float(bucket["latencySeconds"]) for bucket in slots.values()), 3
        ),
    }
    return {"slots": slots, "total": total}


def config_fingerprint(settings: Any, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """评测可复现性的锚（DAT-05）。

    同一份合同今天和明天跑出的指标不该差 5 个点：模型、提示词版本、阈值口径、
    检索参数、思考档位都必须固化在审查记录里，否则版本对比没有意义。
    """
    payload: dict[str, Any] = {
        "llm": {
            "provider": getattr(settings, "llm_provider", ""),
            "model": getattr(settings, "llm_model", ""),
            "reasoningEffort": getattr(settings, "llm_reasoning_effort", ""),
            "maxCharsPerCall": getattr(settings, "llm_max_chars_per_call", 0),
            "maxTokens": getattr(settings, "llm_max_tokens", 0),
            "promptVersion": "",
        },
        "judge": {
            "enabled": getattr(settings, "judge_enabled", False),
            "model": getattr(settings, "judge_model", ""),
            "baseUrl": getattr(settings, "judge_base_url", ""),
            "reasoningEffort": getattr(settings, "judge_reasoning_effort", ""),
            "sameModelPolicy": getattr(settings, "judge_same_model_policy", ""),
            "minConfidence": getattr(settings, "judge_min_confidence", 0),
        },
        "retrieval": {
            "embeddingModel": getattr(settings, "embedding_model", ""),
            "embeddingDimensions": getattr(settings, "embedding_dimensions", 0),
            "topK": getattr(settings, "rag_top_k", 0),
            "minSimilarity": getattr(settings, "rag_min_similarity", 0),
            "sparseEnabled": getattr(settings, "rag_sparse_enabled", False),
            "rerankEnabled": getattr(settings, "rag_rerank_enabled", False),
            "rerankModel": getattr(settings, "rag_rerank_model", ""),
            "rerankMinScore": getattr(settings, "rag_rerank_min_score", 0),
            "maxProvisionsInPrompt": getattr(settings, "max_provisions_in_prompt", 0),
            "mockEmbedding": getattr(settings, "rag_use_mock_embedding", False),
        },
        "routing": {
            "confidenceThreshold": getattr(settings, "confidence_routing_threshold", 0),
            "mandatoryHighRisk": getattr(settings, "mandatory_human_review_for_high_risk", False),
        },
    }
    if extra:
        payload.update(extra)
    return payload
