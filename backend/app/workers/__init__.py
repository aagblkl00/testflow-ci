"""执行模式分发。

M1 默认 inline（无需 Redis，浏览器在独立线程里跑，Web 请求不阻塞）。
后续切换 EXECUTOR_MODE=celery 时，任务体调用的是同一个 execute_run_sync，
因此两种模式下的 CaseResult / StepResult / Artifact 结果完全一致。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..config import settings

logger = logging.getLogger("testflow.workers")

__all__ = ["dispatch_run", "celery_app"]


def _celery_app():  # pragma: no cover - 只有 celery 模式才会走到
    from .celery_app import celery_app

    return celery_app


def _run_inline(run_id: str) -> dict[str, Any]:
    from ..execution.service import execute_run_sync

    return execute_run_sync(run_id)


def dispatch_run(run_id: str, blocking: bool = False) -> dict[str, Any]:
    """把一条执行记录推进队列。

    blocking=False（页面调用）：立即返回，执行在后台线程 / Celery Worker 中进行。
    blocking=True（CLI 调用）：等执行结束再返回，便于拿退出码。
    """
    mode = (settings.executor_mode or "inline").lower()

    if mode == "celery":
        try:
            from .tasks import execute_run_task

            execute_run_task.delay(run_id)
            return {"run_id": run_id, "mode": "celery", "queued": True}
        except Exception as exc:  # noqa: BLE001 - Redis 不可用时必须能降级，不能阻塞 M1
            logger.warning("Celery 投递失败，降级为 inline 执行：%s", exc)
            mode = "inline"

    if blocking:
        return {"mode": "inline", **_run_inline(run_id)}

    def _worker() -> None:
        try:
            _run_inline(run_id)
        except Exception:  # noqa: BLE001 - 后台线程异常只记录，不再向上抛
            logger.exception("inline 执行失败 run_id=%s", run_id)

    thread = threading.Thread(target=_worker, name=f"testflow-run-{run_id}", daemon=True)
    thread.start()
    return {"run_id": run_id, "mode": "inline", "queued": True}
