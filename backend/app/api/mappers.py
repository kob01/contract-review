"""ORM → Schema 映射。

显式写字段而不是靠 `from_attributes` 直转，因为列表页需要"最近一次审查"这类
跨表聚合值，隐式转换会把 N+1 查询藏起来。
"""

from __future__ import annotations

from app.models.contract import Contract
from app.models.enums import ContractKind, ContractStatus
from app.models.human_review import HumanReviewTask
from app.models.knowledge import EnforcementCase, LegalProvision
from app.models.redline import RedLineRule
from app.models.review import ContractReview, ReviewFinding
from app.schemas.contract import ContractBrief, ContractDetail, ContractSegment, MetricEvidence
from app.schemas.knowledge import EnforcementCaseOut, ProvisionOut
from app.schemas.redline import RedLineRuleOut
from app.schemas.review import FindingOut, HumanReviewTaskOut, ReviewStatusOut, ReviewSummary


def _latest_review(contract: Contract) -> ContractReview | None:
    reviews = contract.reviews or []
    return reviews[0] if reviews else None


def to_contract_brief(contract: Contract) -> ContractBrief:
    latest = _latest_review(contract)
    return ContractBrief(
        id=contract.id,
        title=contract.title,
        contract_kind=ContractKind(contract.contract_kind),
        lender_name=contract.lender_name,
        borrower_name=contract.borrower_name,
        file_name=contract.file_name,
        file_suffix=contract.file_suffix,
        file_size_bytes=contract.file_size_bytes,
        page_count=contract.page_count,
        char_count=contract.char_count,
        status=ContractStatus(contract.status),
        created_at=contract.created_at,
        updated_at=contract.updated_at,
        latest_review_id=latest.id if latest else None,
        latest_risk_score=latest.risk_score if latest else None,
        latest_needs_human_review=bool(latest and latest.needs_human_review),
    )


def to_contract_detail(contract: Contract) -> ContractDetail:
    metrics_payload = contract.extracted_metrics or {}
    return ContractDetail(
        **to_contract_brief(contract).model_dump(),
        guarantee_provider=contract.guarantee_provider,
        content_text=contract.content_text,
        segments=[ContractSegment(**segment) for segment in contract.segments or []],
        metrics=[
            MetricEvidence.model_validate(item) for item in metrics_payload.get("evidences", [])
        ],
        derived_metrics=metrics_payload.get("derived", {}),
        metric_warnings=metrics_payload.get("warnings", []),
        parse_error=contract.parse_error,
        owner_id=contract.owner_id,
    )


def to_review_summary(review: ContractReview) -> ReviewSummary:
    return ReviewSummary.model_validate(review)


def to_review_status_out(review: ContractReview) -> ReviewStatusOut:
    """轮询用的轻映射：只取状态与时间戳，不碰 findings 关系。"""
    return ReviewStatusOut(
        id=review.id,
        contract_id=review.contract_id,
        status=review.status,
        elapsed_seconds=review.elapsed_seconds,
        error_message=review.error_message,
        pending_human_count=review.pending_human_count,
        started_at=review.started_at,
        finished_at=review.finished_at,
        heartbeat_at=review.heartbeat_at,
    )


def to_finding_out(finding: ReviewFinding) -> FindingOut:
    return FindingOut(
        id=finding.id,
        risk_level=finding.risk_level,
        clause_category=finding.clause_category,
        title=finding.title,
        excerpt=finding.excerpt,
        analysis=finding.analysis,
        suggestion=finding.suggestion,
        rewritten_text=finding.rewritten_text,
        source=finding.source,
        cross_check_status=finding.cross_check_status,
        rule_code=finding.rule_code,
        metric_snapshot=finding.metric_snapshot or {},
        confidence=finding.confidence,
        llm_confidence=finding.llm_confidence,
        rule_confidence=finding.rule_confidence,
        segment_index=finding.segment_index,
        page_no=finding.page_no,
        char_start=finding.char_start,
        char_end=finding.char_end,
        citations=finding.citations or [],
        similar_case_ids=finding.similar_case_ids or [],
        has_unverified_citation=finding.has_unverified_citation,
        judge_verdict=finding.judge_verdict or {},
        human_review_status=finding.human_review_status,
        human_reviewer_id=finding.human_reviewer_id,
        human_comment=finding.human_comment,
        adjusted_risk_level=finding.adjusted_risk_level,
        order_index=finding.order_index,
    )


def to_task_out(
    task: HumanReviewTask,
    *,
    finding: ReviewFinding | None = None,
    contract_title: str | None = None,
) -> HumanReviewTaskOut:
    return HumanReviewTaskOut(
        id=task.id,
        finding_id=task.finding_id,
        review_id=task.review_id,
        contract_id=task.contract_id,
        reason=task.reason,
        status=task.status,
        assignee=task.assignee,
        comment=task.comment,
        expert_verdict=task.expert_verdict,
        expert_risk_level=task.expert_risk_level,
        created_at=task.created_at,
        resolved_at=task.resolved_at,
        exported_for_sft=task.exported_for_sft,
        finding_title=(finding.title if finding else None) or contract_title,
        finding_excerpt=finding.excerpt if finding else None,
        finding_confidence=finding.confidence if finding else None,
        finding_source=finding.source if finding else None,
        risk_level=finding.risk_level if finding else None,
    )


def to_provision_out(row: LegalProvision) -> ProvisionOut:
    return ProvisionOut(
        id=row.id,
        law_name=row.law_name,
        article_no=row.article_no,
        chapter=row.chapter,
        title=row.title,
        content=row.content,
        doc_type=row.doc_type,
        issuing_body=row.issuing_body,
        effective_on=row.effective_on,
        repealed_on=row.repealed_on,
        version_label=row.version_label,
        source_url=row.source_url,
        is_sample=row.is_sample,
        is_active=row.is_active,
        has_embedding=row.embedding is not None,
    )


def to_case_out(row: EnforcementCase) -> EnforcementCaseOut:
    return EnforcementCaseOut(
        id=row.id,
        case_no=row.case_no,
        org_name=row.org_name,
        penalty_body=row.penalty_body,
        decided_on=row.decided_on,
        summary=row.summary,
        penalty_content=row.penalty_content,
        penalty_amount=row.penalty_amount,
        violation_types=row.violation_types or [],
        basis_refs=row.basis_refs or [],
        source_url=row.source_url,
        is_sample=row.is_sample,
        has_embedding=row.embedding is not None,
    )


def to_rule_out(row: RedLineRule) -> RedLineRuleOut:
    return RedLineRuleOut.model_validate(row)
