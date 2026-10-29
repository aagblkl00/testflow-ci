"""Celery 应用定义。

M1 不依赖 Redis，本模块只在 EXECUTOR_MODE=celery 时才被导入，
因此缺少 redis 依赖或没有 Redis 服务都不会阻塞开发模式。

启动 Worker（Windows 必须 solo pool）：
    celery -A backend.app.workers.celery_app.celery_app worker --pool=solo -l info
"""

from __future__ import annotations

from celery import Celery

from ..config import settings

celery_app = Celery(
    "testflow",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["backend.app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    result_expires=86400,
)
