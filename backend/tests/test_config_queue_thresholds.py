"""配置收口、任务队列提交口径、阈值表与僵尸巡检。

这几处都是"不会报错但会长期出错"的地方：白名单副本常量让配置改了不生效，
BackgroundTasks 让发版丢任务，阈值写死在代码里让法务改不动，
状态机没有收尾让前端永远转圈。因此每一项都要能被单测钉住。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import Settings
from app.services.rules.thresholds import (
    ALLOWED_KEYS,
    Thresholds,
    ThresholdValueError,
    validate_threshold_value,
)
from app.tasks import queue
from app.tasks.queue import IN_PROCESS, QUEUED, REJECTED, enqueue_review, spawn_in_process
from app.tasks.watchdog import _stale_condition, zombie_reason

#: 测试用配置：默认不带 redis/queue 干扰
BASE = {"queue_enabled": False, "redis_url": "redis://localhost:6379/0"}


def build(**overrides) -> Settings:
    payload = {**BASE, **overrides}
    return Settings(**payload)


# ---------- 配置漂移：白名单只有 Settings 一个真理源 ----------


def test_upload_suffixes_are_normalized_and_deduplicated():
    settings = build(allowed_upload_suffixes=["PDF", "docx", ".txt", "pdf"])
    assert settings.allowed_upload_suffixes == [".pdf", ".docx", ".txt"]


def test_upload_suffixes_accept_comma_separated_env_style():
    settings = build(allowed_upload_suffixes="pdf, .docx")
    assert settings.allowed_upload_suffixes == [".pdf", ".docx"]


def test_wildcard_suffix_is_rejected():
    """`*` 这类写法必须被丢掉而不是照单收下：否则"允许任何文件"会静默发生。"""
    with pytest.raises(ValueError):
        build(allowed_upload_suffixes=["*"])


def test_range_and_combination_validators():
    with pytest.raises(ValueError):
        build(confidence_routing_threshold=1.4)
    with pytest.raises(ValueError):
        build(historical_hard_ceiling=0.10, regulatory_cost_ceiling=0.24)
    with pytest.raises(ValueError):
        # 开了队列却给了非 Redis DSN：连上去才会报错太难发现，启动阶段就拒
        build(redis_url="http://localhost:6379", queue_enabled=True)
    # 队列并发远大于模型并发 = 成批 ReadTimeout，而不是吞吐提升
    with pytest.raises(ValueError):
        build(llm_max_concurrency=2, queue_max_jobs=40, queue_enabled=True)


def test_zombie_timeout_never_below_job_timeout():
    settings = build(queue_enabled=True, queue_job_timeout_seconds=2400, zombie_review_timeout_seconds=600)
    assert settings.effective_zombie_timeout_seconds == 2520

    offline = build(queue_enabled=False, zombie_review_timeout_seconds=600)
    assert offline.effective_zombie_timeout_seconds == 600


def test_audit_warnings_surface_known_degradations():
    settings = build(
        rag_use_mock_embedding=True,
        llm_api_key="",
        judge_enabled=True,
        judge_model="glm-5.3-flash",
        llm_model="glm-5.3-flash",
        # 显式同端点：这条用例测的就是"同来路复核"这一种降级
        judge_base_url="",
        judge_api_key="",
    )
    warnings = " ".join(settings.audit_warnings())
    assert "mock 向量" in warnings
    assert "不是独立信号" in warnings


def test_audit_warnings_surface_foreign_judge_without_own_key():
    """跨供应商复核缺密钥要说出来：否则 /health 看着正常，其实一次复核都没跑。"""
    settings = build(
        llm_api_key="sk-test",
        llm_model="glm-5.3-flash",
        judge_enabled=True,
        rag_use_mock_embedding=False,
        judge_base_url="https://api.deepseek.com/v1",
        judge_model="deepseek-flash",
        judge_api_key="",
    )
    warnings = " ".join(settings.audit_warnings())
    assert "不执行结论复核" in warnings


# ---------- 任务提交口径 ----------


async def test_enqueue_uses_process_fallback_when_queue_disabled(monkeypatch):
    captured: list[tuple] = []
    monkeypatch.setattr(
        queue,
        "spawn_in_process",
        lambda review_id, request: captured.append((review_id, request)) or IN_PROCESS,
    )
    review_id = uuid.uuid4()
    outcome = await enqueue_review(review_id, _request(), build(queue_enabled=False))
    assert outcome == IN_PROCESS
    assert captured and captured[0][0] == review_id


async def test_enqueue_rejects_when_fallback_disabled():
    outcome = await enqueue_review(
        uuid.uuid4(), _request(), build(queue_enabled=False, queue_fallback_in_process=False)
    )
    assert outcome == REJECTED


async def test_enqueue_marks_queued_when_pool_available(monkeypatch):
    class FakePool:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def enqueue_job(self, function, *args, **kwargs):
            self.calls.append({"function": function, "args": args, "kwargs": kwargs})
            return object()

    pool = FakePool()

    async def fake_pool(_settings):
        return pool

    monkeypatch.setattr(queue, "shared_queue_pool", fake_pool)
    review_id = uuid.uuid4()
    outcome = await enqueue_review(review_id, _request(), build(queue_enabled=True))

    assert outcome == QUEUED
    call = pool.calls[0]
    assert call["function"] == queue.REVIEW_JOB_NAME
    assert call["args"][0] == str(review_id)
    # _job_id 用 review_id：重复点击时 ARQ 返回既有任务，不会排两轮
    assert call["kwargs"]["_job_id"] == f"review:{review_id}"


async def test_enqueue_falls_back_when_redis_unreachable(monkeypatch):
    async def broken_pool(_settings):
        return None

    captured: list = []
    monkeypatch.setattr(queue, "shared_queue_pool", broken_pool)
    monkeypatch.setattr(
        queue,
        "spawn_in_process",
        lambda review_id, request: captured.append(review_id) or IN_PROCESS,
    )
    review_id = uuid.uuid4()
    outcome = await enqueue_review(review_id, _request(), build(queue_enabled=True))
    assert outcome == IN_PROCESS
    assert captured == [review_id]


async def test_spawn_in_process_keeps_task_reference():
    """不持有引用的 asyncio 任务可能被垃圾回收后静默消失，这里必须能数出来。"""

    async def fake_job(ctx, review_id, request):
        del ctx, review_id, request
        await asyncio.sleep(30)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(queue, "run_review_job", fake_job)
    before = len(queue._spawned)
    try:
        outcome = spawn_in_process(uuid.uuid4(), _request())
        assert outcome == IN_PROCESS
        assert len(queue._spawned) == before + 1
    finally:
        # 收尾：清掉测试自己发的后台任务，不留“Task was destroyed but it is pending”
        await queue.drain_in_process_tasks(0.01)
        monkeypatch.undo()
        await queue.drain_in_process_tasks(0.01)


def _request():
    from app.schemas.review import ReviewRunRequest

    return ReviewRunRequest(perspective="consumer")


# ---------- 阈值表 ----------


def test_threshold_value_range_validation():
    validate_threshold_value("lpr_one_year_rate", 0.031)
    with pytest.raises(ThresholdValueError):
        validate_threshold_value("lpr_one_year_rate", 3.1)  # 百分数误写进 ratio 口径
    with pytest.raises(ThresholdValueError):
        validate_threshold_value("not_a_key", 0.5)


def test_default_lpr_is_the_current_published_rate():
    """默认值是业务参数不是常量：一年期 LPR 自 2025-05-20 起为 3.0%。

    曾经长期停在 3.1%（2024-10 那一档），4 倍上限被算成 12.4% 而不是 12.0%，
    民间借贷合同里 12.0%~12.4% 这一段会被整片放过。
    """
    default = Settings.model_fields["lpr_one_year_rate"].default
    assert default == pytest.approx(0.030)
    assert default * Settings.model_fields["lpr_multiple"].default == pytest.approx(0.12)


def test_thresholds_from_settings_marks_every_source_as_settings():
    settings = build()
    loaded = Thresholds.from_settings(settings)
    assert loaded.judicial_protection_ceiling == round(
        settings.lpr_one_year_rate * settings.lpr_multiple, 6
    )
    assert set(loaded.sources) == set(ALLOWED_KEYS)
    assert all(source == "settings" for source in loaded.sources.values())


def test_threshold_rows_are_editable_ready():
    rows = Thresholds.from_settings(build()).rows()
    assert {row["key"] for row in rows} == set(ALLOWED_KEYS)
    assert all(row["max"] > row["min"] and row["label"] for row in rows)


# ---------- 僵尸巡检 ----------


def test_stale_condition_falls_back_to_updated_at():
    """心跳缺失时必须回退到行更新时间，否则从未被受理的任务永远查不出来。"""
    sql = str(_stale_condition(datetime.now(UTC)).compile(compile_kwargs={"literal_binds": True}))
    assert "COALESCE" in sql.upper()
    assert "heartbeat_at" in sql
    assert "updated_at" in sql


def test_zombie_reason_differs_never_started_from_crashed():
    from app.models.enums import ReviewStatus

    pending = zombie_reason(ReviewStatus.PENDING.value, 3600)
    running = zombie_reason(ReviewStatus.RUNNING.value, 3600)
    assert "worker" in pending and "未" in pending
    assert "崩溃" in running or "重新发起" in running
    assert "60" in pending  # 阈值以分钟呈现，运维不用自己换算


def test_stale_window_excludes_live_heartbeat():
    """刚刷过心跳的任务不该被查出来：这是"慢任务"与"死任务"的分界。"""
    cutoff = datetime.now(UTC) - timedelta(seconds=3600)
    stmt_sql = str(_stale_condition(cutoff).compile())
    assert "<=" in stmt_sql or "<" in stmt_sql
