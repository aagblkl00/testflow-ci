from __future__ import annotations

from fastapi import APIRouter

from . import alerts, analyses, auth, cases, ci, failures, flows, plans, projects, retention, runs, suites

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(cases.router)
api_router.include_router(flows.router)
api_router.include_router(suites.router)
api_router.include_router(plans.router)
api_router.include_router(runs.router)
api_router.include_router(failures.router)
api_router.include_router(ci.router)
# M5：AI 分析与告警通知。放在最后，不改变既有路由的注册顺序。
api_router.include_router(analyses.router)
api_router.include_router(alerts.router)
api_router.include_router(retention.router)


@api_router.get("/api/health", tags=["system"])
def health() -> dict[str, object]:
    from ..config import settings

    return {
        "status": "ok",
        "app": "TestFlow CI",
        "version": "0.1.0-m1",
        "executor_mode": settings.executor_mode,
        "database": "sqlite" if settings.is_sqlite else "postgresql",
        "storage_dir": str(settings.storage_dir),
    }


__all__ = ["api_router"]
