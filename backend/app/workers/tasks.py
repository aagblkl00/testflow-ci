"""Celery 任务定义。

任务体与 inline 模式共用 execute_run_sync，因此切换 EXECUTOR_MODE 不会改变结果语义。
"""

from __future__ import annotations

from typing import Any

from .celery_app import celery_app


@celery_app.task(name="testflow.execute_run", bind=True, max_retries=0)
def execute_run_task(self, run_id: str) -> dict[str, Any]:  # noqa: ANN001 - celery 注入 self
    from ..execution.service import execute_run_sync

    return execute_run_sync(run_id)
