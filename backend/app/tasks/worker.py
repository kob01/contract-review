"""审查 worker 进程入口。

    python -m app.tasks.worker            # 常驻消费
    python -m app.tasks.worker --burst    # 把队列里的清完就退出（本地一次性验证）

为什么必须是独立进程：审查任务分钟级，和 API 同进程就意味着"发版即丢任务"。
独立进程后 API 可以随便重启，在跑的任务由 worker 持有，进程死了也有巡检收尾。
"""

from __future__ import annotations

import argparse

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.tasks.queue import build_worker_settings
from arq.worker import create_worker

logger = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="信贷合同审查任务 worker")
    parser.add_argument(
        "--burst",
        action="store_true",
        help="消费完当前队列即退出，用于本地验证入队链路是否通",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=None,
        help="覆盖 QUEUE_MAX_JOBS（同时处理的审查数）",
    )
    return parser.parse_args()


def main() -> None:
    setup_logging()
    args = _parse_args()
    app_settings = get_settings()

    kwargs: dict = {}
    if args.burst:
        kwargs["burst"] = True
    if args.max_jobs is not None:
        kwargs["max_jobs"] = args.max_jobs

    logger.info(
        "启动审查 worker：队列=%s Redis=%s 并发=%s 硬超时=%ss%s",
        app_settings.queue_name,
        app_settings.redis_url,
        args.max_jobs or app_settings.queue_max_jobs,
        app_settings.queue_job_timeout_seconds,
        "（burst 模式：清完即退）" if args.burst else "",
    )
    # create_worker 内部会自己装日志处理器，所以先记下我们的口径再交给它
    create_worker(build_worker_settings(app_settings), **kwargs).run()


if __name__ == "__main__":
    main()
