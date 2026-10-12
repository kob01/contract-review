"""合规红线规则接口：查询、维护、阈值展示与规则试算。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Settings, get_db, get_settings
from app.api.mappers import to_rule_out
from app.models.enums import ClauseCategory, ContractKind, RiskLevel
from app.models.redline import RedLineRule
from app.schemas.common import CamelModel
from app.schemas.redline import (
    RedLineRuleOut,
    RedLineRuleUpsertRequest,
    RuleDryRunRequest,
    RuleDryRunResponse,
    RuleHitOut,
    ThresholdSnapshot,
    ThresholdUpdateRequest,
)
from app.services.parser.extractor import ExtractedDocument, PageText
from app.services.parser.segmenter import segmentize
from app.services.rules.engine import RuleEngine
from app.services.rules.metrics import extract_metrics
from app.services.rules.seed import load_active_rules
from app.services.rules.thresholds import (
    Thresholds,
    ThresholdValueError,
    load_thresholds,
    upsert_thresholds,
)

router = APIRouter(prefix="/redlines", tags=["redlines"])


class RuleToggleRequest(CamelModel):
    is_active: bool


@router.get("", response_model=list[RedLineRuleOut])
async def list_rules(
    category: ClauseCategory | None = None,
    contract_kind: ContractKind | None = None,
    include_inactive: bool = Query(default=True),
    session: AsyncSession = Depends(get_db),
) -> list[RedLineRuleOut]:
    stmt = select(RedLineRule).order_by(RedLineRule.priority, RedLineRule.code)
    if not include_inactive:
        stmt = stmt.where(RedLineRule.is_active)
    if category is not None:
        stmt = stmt.where(RedLineRule.category == category.value)

    rows = await session.execute(stmt)
    rules = list(rows.scalars().all())
    if contract_kind is not None:
        rules = [
            rule
            for rule in rules
            if not rule.applies_to or contract_kind.value in rule.applies_to
        ]
    return [to_rule_out(rule) for rule in rules]


@router.post("", response_model=RedLineRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RedLineRuleUpsertRequest, session: AsyncSession = Depends(get_db)
) -> RedLineRuleOut:
    """新增自定义红线。内置规则编码不允许被占用。"""
    existing = await session.scalar(
        select(RedLineRule).where(RedLineRule.code == payload.code)
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"规则编码 {payload.code} 已存在"
            + ("（内置规则请改用 PATCH 调整）" if existing.is_builtin else ""),
        )

    rule = RedLineRule(**_rule_fields(payload))
    session.add(rule)
    await session.commit()
    return to_rule_out(rule)


@router.patch("/{code}", response_model=RedLineRuleOut)
async def update_rule(
    code: str,
    payload: RedLineRuleUpsertRequest,
    session: AsyncSession = Depends(get_db),
) -> RedLineRuleOut:
    rule = await _get_rule(code, session)
    fields = _rule_fields(payload)
    fields.pop("code", None)
    for field, value in fields.items():
        setattr(rule, field, value)
    await session.commit()
    return to_rule_out(rule)


@router.post("/{code}/toggle", response_model=RedLineRuleOut)
async def toggle_rule(
    code: str,
    payload: RuleToggleRequest,
    session: AsyncSession = Depends(get_db),
) -> RedLineRuleOut:
    """停用一条内置规则是常见诉求（例如某类产品不适用某口径）。"""
    rule = await _get_rule(code, session)
    rule.is_active = payload.is_active
    await session.commit()
    return to_rule_out(rule)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(code: str, session: AsyncSession = Depends(get_db)) -> None:
    rule = await _get_rule(code, session)
    if rule.is_builtin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="内置红线不可删除，请改用 toggle 停用",
        )
    await session.delete(rule)
    await session.commit()


@router.get("/thresholds", response_model=ThresholdSnapshot)
async def thresholds(
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> ThresholdSnapshot:
    """当前生效的阈值口径（库内记录优先于环境变量）。

    报告页必须展示这几个数：同一份合同在 LPR 调整后结论会不同，
    不展示口径等于把判断标准藏起来。
    """
    loaded = await load_thresholds(session, app_settings)
    return _snapshot(loaded)


@router.put("/thresholds", response_model=ThresholdSnapshot)
async def update_thresholds(
    payload: ThresholdUpdateRequest,
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> ThresholdSnapshot:
    """批量调整阈值口径，不用改代码、不用重发版。

    内置规则的“能不能改”有红线库自己的约束（不可删除、需 toggle），
    但这几个行情数本身就是需要定期维护的数据，不应靠发布流程驱动。
    """
    updates = [(item.key, item.value, item.effective_on) for item in payload.items]
    try:
        loaded = await upsert_thresholds(
            session, updates, updated_by=payload.updated_by.strip() or "legal", settings=app_settings
        )
    except ThresholdValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return _snapshot(loaded)


def _snapshot(loaded: Thresholds) -> ThresholdSnapshot:
    return ThresholdSnapshot(
        lpr_one_year_rate=loaded.lpr_one_year_rate,
        lpr_multiple=loaded.lpr_multiple,
        judicial_protection_ceiling=loaded.judicial_protection_ceiling,
        regulatory_cost_ceiling=loaded.regulatory_cost_ceiling,
        historical_hard_ceiling=loaded.historical_hard_ceiling,
        confidence_routing_threshold=loaded.confidence_routing_threshold,
        items=loaded.rows(),
    )


@router.post("/dry-run", response_model=RuleDryRunResponse)
async def dry_run(
    payload: RuleDryRunRequest,
    session: AsyncSession = Depends(get_db),
    app_settings: Settings = Depends(get_settings),
) -> RuleDryRunResponse:
    """对一段文本试跑规则库，不产生审查记录、不调用模型。

    这是调阈值的主要工作界面：改一个 `threshold_value`，
    立刻能看到它对样本合同的影响。
    """
    if not payload.text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文本不能为空")

    document = ExtractedDocument(
        text=payload.text,
        pages=[PageText(number=1, text=payload.text, char_start=0)],
    )
    segments = segmentize(document)
    metrics = extract_metrics(document, segments)

    rules = await load_active_rules(session)
    if payload.rule_codes:
        wanted = set(payload.rule_codes)
        rules = [rule for rule in rules if rule.code in wanted]

    # 试算跟真实审查同一个口径入口：改完阈值当场能看到它对样本合同的影响
    thresholds = await load_thresholds(session, app_settings)
    evaluation = RuleEngine(rules, app_settings, thresholds).evaluate(
        metrics,
        full_text=document.text,
        segments=segments,
        contract_kind=payload.contract_kind.value,
    )

    return RuleDryRunResponse(
        hits=[
            RuleHitOut(
                rule_code=hit.rule_code,
                rule_name=hit.rule_name,
                severity=RiskLevel(hit.severity),
                category=ClauseCategory(hit.category),
                hard_block=hit.hard_block,
                title=hit.title,
                analysis=hit.analysis,
                suggestion=hit.suggestion,
                confidence=hit.confidence,
                needs_llm_review=hit.needs_llm_review,
                metric_key=hit.metric_key,
                metric_value=hit.metric_value,
                threshold=hit.threshold,
                threshold_desc=hit.threshold_desc,
                derivation=hit.derivation,
                excerpt=hit.evidence.excerpt if hit.evidence else "",
                page_no=hit.evidence.page_no if hit.evidence else None,
                segment_index=hit.evidence.segment_index if hit.evidence else None,
                basis_refs=hit.basis_refs,
            )
            for hit in evaluation.hits
        ],
        inconclusive=[{"code": code, "name": name} for code, name in evaluation.inconclusive],
        weak_signals=evaluation.weak_signals,
        checked_rule_count=evaluation.checked_rule_count,
        metrics=metrics.to_dict(),
    )


def _rule_fields(payload: RedLineRuleUpsertRequest) -> dict:
    return {
        "code": payload.code,
        "name": payload.name,
        "category": payload.category.value,
        "severity": payload.severity.value,
        "applies_to": [kind.value for kind in payload.applies_to],
        "metric_key": payload.metric_key,
        "operator": payload.operator.value if payload.operator else None,
        "threshold_source": payload.threshold_source,
        "threshold_value": payload.threshold_value,
        "unit": payload.unit,
        "tolerance": payload.tolerance,
        "secondary_threshold": payload.secondary_threshold,
        "keyword_patterns": payload.keyword_patterns,
        "hard_block": payload.hard_block,
        "llm_fallback": payload.llm_fallback,
        "hit_confidence": payload.hit_confidence,
        "basis_refs": payload.basis_refs,
        "guidance_text": payload.guidance_text,
        "suggestion_text": payload.suggestion_text,
        "priority": payload.priority,
        "is_active": payload.is_active,
    }


async def _get_rule(code: str, session: AsyncSession) -> RedLineRule:
    rule = await session.scalar(select(RedLineRule).where(RedLineRule.code == code))
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"规则 {code} 不存在")
    return rule
