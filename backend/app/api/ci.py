"""M4 CI 触发与状态查询接口。

三个入口，共用同一条执行链路：

    POST /api/ci/runs                通用 CI 触发（CLI / 脚本 / 任意 CI）
    POST /api/ci/gitlab/trigger      GitLab Pipeline / Webhook 触发
    GET  /api/runs/{run_id}/ci-status  CI 轮询执行结果

三者最后都汇到 `ci.service.create_ci_run` → `execution.service.create_run`
→ `workers.dispatch_run` → `execute_run_sync`。**没有任何一处复制执行逻辑。**

鉴权用 CI Token（`X-TestFlow-Token`，GitLab Webhook 也接受 `X-Gitlab-Token`），
与网页端的 JWT 完全独立：CI 机器拿不到、也不该拿用户的登录态。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ci import service as ci_service
from ..ci.auth import require_ci_token
from ..ci.gitlab import parse_gitlab_payload
from ..ci.schemas import CiRunRequest
from ..db import get_db
from ..models import Environment, ExecutionRun, Project, RegressionPlan
from ..workers import dispatch_run

logger = logging.getLogger("testflow.api.ci")

router = APIRouter(prefix="/api", tags=["ci"])

# 调用方声明的 source → Run.trigger_source（沿用现有状态词，不发明新名字）
SOURCE_TO_TRIGGER = {"gitlab": "GITLAB", "cli": "CLI", "ci": "CI"}
DEFAULT_TRIGGER_SOURCE = "CI"


def _trigger_source(raw: str) -> str:
    return SOURCE_TO_TRIGGER.get(str(raw or "").strip().lower(), DEFAULT_TRIGGER_SOURCE)


def _to_http(exc: ci_service.CiRequestError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _dispatch_or_existing(
    db: Session,
    *,
    project_ref: str,
    plan_ref: str,
    environment_ref: str,
    trigger_source: str,
    pipeline_id: str = "",
    pipeline_url: str = "",
    commit_sha: str = "",
    branch: str = "",
    base_url: str = "",
) -> dict[str, Any]:
    """两个触达入口共用的流程：解析 → 预检 → 幂等创建 → 派发。"""
    try:
        project_ref = str(project_ref or "").strip()
        if project_ref:
            project = ci_service.resolve_project(db, project_ref)
            plan = ci_service.resolve_plan(db, project.id, plan_ref)
        else:
            # 允许省略项目：`--plan smoke --env test` 是 CI 里最自然的写法。
            # 但只在计划能唯一定位时才放行（同名多个会报 400 让人显式指定）。
            plan = ci_service.resolve_plan_any_project(db, plan_ref)
            project = db.get(Project, plan.project_id)
            if project is None:  # pragma: no cover - 外键保证不会发生
                raise ci_service.CiRequestError(404, "计划所属项目不存在")
        environment = ci_service.resolve_environment(db, project.id, environment_ref)
        blocked = ci_service.preflight_environment(environment, base_url)
        if blocked:
            # 环境体检不通过就别建 Run 了 —— 建了必然 ERROR，只是给失败中心添噪音。
            # 安全门禁本身仍在执行时再校验一次（这里是提前拦，不是替换）。
            raise ci_service.CiRequestError(400, "环境未通过安全门禁：" + "；".join(blocked))
        run, created = ci_service.create_ci_run(
            db,
            project=project,
            plan=plan,
            environment=environment,
            trigger_source=trigger_source,
            pipeline_id=pipeline_id,
            pipeline_url=pipeline_url,
            commit_sha=commit_sha,
            branch=branch,
            base_url=base_url,
        )
    except ci_service.CiRequestError as exc:
        raise _to_http(exc) from None

    if created:
        # 非阻塞派发：CI 侧自己轮询 ci-status，不占着 HTTP 连接等一次完整的浏览器回归。
        dispatch_run(run.id, blocking=False)

    db.refresh(run)
    payload = ci_service.ci_status_payload(db, run)
    payload["created"] = created
    payload["idempotent_hit"] = not created
    payload["ci_status_url"] = f"/api/runs/{run.id}/ci-status"
    return payload


@router.post("/ci/runs")
def create_ci_run(
    payload: CiRunRequest,
    _: str = Depends(require_ci_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """CI 触发一次回归执行。幂等键 (project, plan, pipeline_id)。"""
    return _dispatch_or_existing(
        db,
        project_ref=payload.project_ref(),
        plan_ref=payload.plan_ref(),
        environment_ref=payload.environment_ref(),
        trigger_source=_trigger_source(payload.source),
        pipeline_id=payload.pipeline_id,
        pipeline_url=payload.pipeline_url,
        commit_sha=payload.commit_sha,
        branch=payload.branch,
        base_url=payload.base_url,
    )


@router.post("/ci/gitlab/trigger")
def gitlab_trigger(
    payload: dict[str, Any] = Body(default_factory=dict),
    x_gitlab_event: str | None = Header(default=None, alias="X-Gitlab-Event"),
    _: str = Depends(require_ci_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """GitLab 触发。同时支持原生 Pipeline Webhook 与 CI Job 直接调用。

    请求体刻意用裸 dict 而不是 Pydantic 模型：真实的 GitLab Pipeline Webhook
    里 `project` 是一个对象（含 id / path_with_namespace / web_url），
    用固定字段的模型去收会在 422 上直接挂掉 —— 而 Webhook 是由 GitLab 发的，
    它不会按我们的模型改载荷。校验放到 `parse_gitlab_payload` 里，
    只对我们真正需要的字段做判断。
    """
    try:
        trigger = parse_gitlab_payload(payload, event_header=str(x_gitlab_event or ""))
    except ci_service.CiRequestError as exc:
        raise _to_http(exc) from None

    logger.info("GitLab 触发：%s", trigger.as_dict())
    return _dispatch_or_existing(
        db,
        project_ref=trigger.project_ref,
        plan_ref=trigger.plan_ref,
        environment_ref=trigger.environment_ref,
        trigger_source="GITLAB",
        pipeline_id=trigger.pipeline_id,
        pipeline_url=trigger.pipeline_url,
        commit_sha=trigger.commit_sha,
        branch=trigger.branch,
    )


@router.get("/runs/{run_id}/ci-status")
def run_ci_status(
    run_id: str,
    _: str = Depends(require_ci_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """CI 轮询入口。未结束时 `finished=false`、`exit_code=null`。

    刻意不在 Run 未结束时返回退出码：调用方必须自己决定等多久，
    超时是**调用方**的判断，不能由服务端替它给一个「看起来像结论」的数字。
    """
    run = db.get(ExecutionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    payload = ci_service.ci_status_payload(db, run)
    payload["ci_status_url"] = f"/api/runs/{run_id}/ci-status"
    return payload


@router.get("/ci/meta")
def ci_meta(
    _: str = Depends(require_ci_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """给 CLI 用的元信息：能解析到的项目/计划/环境清单与退出码含义。

    存在的理由很实际：`testflow run --plan smoke --env test` 失败时，
    报「计划不存在」远不如把当前可用的名字列出来有用。
    """
    items = []
    for project in db.execute(select(Project)).scalars().all():
        plans = db.execute(select(RegressionPlan).where(RegressionPlan.project_id == project.id)).scalars().all()
        environments = db.execute(
            select(Environment).where(Environment.project_id == project.id)
        ).scalars().all()
        items.append(
            {
                "project_id": project.id,
                "project": project.name,
                "plans": [{"id": item.id, "name": item.name, "enabled": bool(item.enabled)} for item in plans],
                "environments": [
                    {"id": item.id, "name": item.name, "type": item.environment_type} for item in environments
                ],
            }
        )
    return {
        "exit_codes": {
            "0": "TestFlowCI 完成执行，全部用例通过",
            "1": "TestFlowCI 完成执行，但存在未通过的用例（含被测环境问题）",
            "2": "TestFlowCI 自身未能完成可靠执行（平台侧问题）",
        },
        "idempotency_key": ["project_id", "plan_id", "pipeline_id"],
        "projects": items,
    }
