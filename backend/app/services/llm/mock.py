"""离线 Mock 供应商（合规审查版）。

没有 LLM 密钥时启用，用于：
1. 首次克隆仓库即可跑通"上传 → 解析 → 指标核算 → 检索 → 双通道校验 → 复核队列"；
2. 单元测试与前端联调不依赖网络。

它不是"假装智能"：只做正则命中，且刻意把置信度压在 0.7 左右，
让置信度路由真实地把结论推进人工复核队列，从而验证治理链路而不是掩盖它。
引用的 provisionId 只从提示词给出的清单里取，清单为空时不编造引用。
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.models.enums import ClauseCategory
from app.services.llm.base import LLMResult, LLMUsage
from app.services.rules.metrics import FLAG_PATTERNS  # 与规则引擎共用同一份特征表

_PROVISION_RE = re.compile(r"- \[#(?P<pid>[0-9a-fA-F-]+)\] 《(?P<law>[^》]+)》(?P<article>\S*)")
_METRIC_RATE_RE = re.compile(r"(?P<label>[^：:]{0,24})：(?P<value>[0-9.]+)%")
#: 防注入分隔标签下的单条条款正文；逐块扫描才能保证 excerpt 是原文片段而不是跨条款窗口
_CLAUSE_BLOCK_RE = re.compile(
    r'<clause index="(?P<index>[0-9]+)"[^>]*>(?P<body>.*?)</clause>', re.DOTALL
)

#: 特征键 → 条款分类
_CATEGORY_BY_KEY: dict[str, str] = {
    "blank_deduction_authorization": ClauseCategory.CONSUMER_PROTECTION.value,
    "waive_procedural_rights": ClauseCategory.CONSUMER_PROTECTION.value,
    "unilateral_price_change": ClauseCategory.CONSUMER_PROTECTION.value,
    "final_interpretation": ClauseCategory.CONSUMER_PROTECTION.value,
    "broad_info_sharing": ClauseCategory.PERSONAL_INFO.value,
    "mandatory_insurance": ClauseCategory.TIED_PRODUCT.value,
    "auto_rollover": ClauseCategory.REPAYMENT.value,
    "lender_home_jurisdiction": ClauseCategory.DISPUTE.value,
    "acceleration_broad": ClauseCategory.BREACH_PENALTY.value,
    "third_party_collection": ClauseCategory.BREACH_PENALTY.value,
    "head_interest_deduction": ClauseCategory.RATE_AND_FEE.value,
}

_HIGH_RISK_KEYS = {
    "waive_procedural_rights",
    "final_interpretation",
    "mandatory_insurance",
    "head_interest_deduction",
    "broad_info_sharing",
}

#: 关键词 → 优先引用的法规名片段，用于从提示词清单中挑一条最相关的
_LAW_HINTS: tuple[tuple[str, str], ...] = (
    ("年化", "年化"),
    ("利率", "民间借贷"),
    ("利息", "民法典"),
    ("保险", "消费者权益保护"),
    ("个人信息服务", "个人信息"),
    ("催收", "消费者权益保护"),
    ("格式", "消费者权益保护"),
)


class MockLLMProvider:
    name = "mock"

    def __init__(self, model_label: str = "mock-compliance-heuristic") -> None:
        self.model = model_label
        #: 启发式不调模型，没有思考深度可言；占位值避免下游把它当成真实档位
        self.reasoning_effort = "none"

    async def complete_json(self, *, system_prompt: str, user_prompt: str) -> LLMResult:
        started = time.perf_counter()

        provisions = self._parse_provisions(system_prompt)
        metric_lines = self._parse_metrics(system_prompt)
        findings = self._scan(user_prompt, provisions, metric_lines)

        payload: dict[str, Any] = {
            "batchSummary": (
                f"本批次以启发式规则命中 {len(findings)} 处可疑表述，"
                "置信度按启发式上限给出，需人工确认。"
                if findings
                else "本批次未发现可疑表述。"
            ),
            "findings": findings,
        }

        return LLMResult(
            payload=payload,
            model=self.model,
            provider=self.name,
            latency_seconds=round(time.perf_counter() - started, 4),
            usage=LLMUsage(
                prompt_tokens=len(system_prompt + user_prompt) // 2,
                completion_tokens=len(findings) * 140,
                total_tokens=len(system_prompt + user_prompt) // 2 + len(findings) * 140,
            ),
        )

    async def aclose(self) -> None:
        """无连接可释放；保留接口一致性，让上层可以无条件关闭。"""
        return None

    # ---------- 提示词解析 ----------

    @staticmethod
    def _parse_provisions(system_prompt: str) -> list[dict[str, str]]:
        return [
            {"provisionId": m["pid"], "lawName": m["law"], "articleNo": m["article"] or "全文"}
            for m in _PROVISION_RE.finditer(system_prompt)
        ]

    @staticmethod
    def _parse_metrics(system_prompt: str) -> list[tuple[str, float]]:
        block_match = re.search(r"# 已知事实.*?\n(.*?)\n# ", system_prompt, re.DOTALL)
        if not block_match:
            return []
        rows: list[tuple[str, float]] = []
        for match in _METRIC_RATE_RE.finditer(block_match.group(1)):
            try:
                rows.append((match["label"].strip(), float(match["value"]) / 100.0))
            except ValueError:
                continue
        return rows

    # ---------- 命中逻辑 ----------

    def _scan(
        self,
        user_prompt: str,
        provisions: list[dict[str, str]],
        metrics: list[tuple[str, float]],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []

        # 新提示词把条款装在 <clause> 里：逐块扫描，命中窗口不会跨到下一条条款，
        # excerpt 也就仍是能在原文里逐字找到的片段
        bodies = [match.group("body") for match in _CLAUSE_BLOCK_RE.finditer(user_prompt)]
        scanned = bodies or [user_prompt]

        for text in scanned:
            for key, label, pattern, guidance in FLAG_PATTERNS:
                match = re.search(pattern, text)
                if not match:
                    continue
                excerpt = self._excerpt_around(text, match)
                findings.append(
                    self._finding(
                        risk_level="high" if key in _HIGH_RISK_KEYS else "medium",
                        category=_CATEGORY_BY_KEY.get(key, ClauseCategory.OTHER.value),
                        title=f"{label}（启发式命中）",
                        excerpt=excerpt,
                        analysis=f"{guidance} 本结论由离线 Mock 供应商给出，未经模型语义判断。",
                        suggestion="由法务确认条款表述是否属于监管禁止或失衡安排，并按红线库建议改写。",
                        confidence=0.7,
                        provisions=provisions,
                        hint=label,
                    )
                )

        for label, value in metrics:
            if value <= 0.24 or "年化" not in label:
                continue
            findings.append(
                self._finding(
                    risk_level="high",
                    category=ClauseCategory.RATE_AND_FEE.value,
                    title="综合融资成本超过 24% 监管红线",
                    excerpt=f"{label} {value * 100:.2f}%",
                    analysis=(
                        f"规则引擎核算的{label}为 {value * 100:.2f}%，超过 24% 红线口径。"
                        "离线 Mock 仅复述指标，具体定性以规则引擎结果为准。"
                    ),
                    suggestion="压降利率与费用合并口径至红线以内，并以年化形式明示。",
                    confidence=0.95,
                    provisions=provisions,
                    hint="年化",
                )
            )

        return findings

    @staticmethod
    def _excerpt_around(text: str, match: re.Match[str], window: int = 50) -> str:
        start = max(match.start() - window, 0)
        end = min(match.end() + window, len(text))
        return re.sub(r"\s+", " ", text[start:end]).strip()

    @staticmethod
    def _pick_provision(provisions: list[dict[str, str]], hint: str) -> list[dict[str, Any]]:
        """从提示词清单里挑一条引用；挑不到就返回空数组，绝不编造。"""
        if not provisions:
            return []

        wanted = next((keyword for keyword, law_hint in _LAW_HINTS if keyword in hint), None)
        if wanted:
            for provision in provisions:
                if wanted in provision["lawName"]:
                    return [
                        {
                            "provisionId": provision["provisionId"],
                            "lawName": provision["lawName"],
                            "articleNo": provision["articleNo"],
                            "quote": "",
                        }
                    ]
        first = provisions[0]
        return [
            {
                "provisionId": first["provisionId"],
                "lawName": first["lawName"],
                "articleNo": first["articleNo"],
                "quote": "",
            }
        ]

    def _finding(
        self,
        *,
        risk_level: str,
        category: str,
        title: str,
        excerpt: str,
        analysis: str,
        suggestion: str,
        confidence: float,
        provisions: list[dict[str, str]],
        hint: str,
    ) -> dict[str, Any]:
        segment_index = None  # 由审查管道按原文定位回填，模型侧不自行推断
        return {
            "riskLevel": risk_level,
            "clauseCategory": category,
            "title": title,
            "excerpt": excerpt,
            "segmentIndex": segment_index,
            "analysis": analysis,
            "suggestion": suggestion,
            "rewrittenText": None,
            "confidence": confidence,
            "citations": self._pick_provision(provisions, hint),
            "ruleCode": None,
        }
