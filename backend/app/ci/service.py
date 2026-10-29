"""CI 触发的服务层：解析、幂等、结果映射。

三个必须说清楚的设计点：

**1. Exit Code 只由 Run 最终状态决定，不看 FailureGroup.category。**
`ENVIRONMENT_ERROR` 是「测试跑完了并发现环境有问题」，属于 Exit 1；
只有 TestFlowCI 自己没能完成可靠执行（Run=ERROR、API 连不上、参数非法、
等待超时）才是 Exit 2。这条边界写错一次，CI 就会把「被测环境挂了」误报成
「测试平台挂了」，或者反过来把平台故障当成测试失败放过去。

**2. 幂等键 = (project_id, plan_id, pipeline_id)，并且必须有数据库唯一约束。**
只靠 `if not exists` 在并发下必然漏：两个请求同时查到「不存在」，然后各建一条。
唯一索引在 models 里带 WHERE 条件（只约束 CI 触发来源），详见那里的注释。

**3. 解析参数支持「ID 或名称」，但必须在同一个项目内解析。**
允许 GitLab 直接塞一个 SQL ID 就跑，等于把执行权限交给一个字符串；
所以计划与环境都先判归属，不属于该项目的一律 404。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..execution.service import create_run
from ..models import (
    CI_TRIGGER_SOURCES,
    Environment,
    ExecutionRun,
    FailureGroup,
    FailureOccurrence,
    Project,
    RegressionPlan,
)
from ..utils.redact import redact_text

logger = logging.getLogger("testflow.ci")

# ------------------------------------------------------------------ Exit Code

EXIT_PASSED = 0
EXIT_TEST_FAILURE = 1
EXIT_SYSTEM_ERROR = 2

FINISHED_RUN_STATUSES = frozenset({"PASSED", "FAILED", "PARTIAL", "CANCELLED", "ERROR"})


def run_exit_code(status: str) -> int | None:
    """Run 状态 → CI 退出码。未结束时返回 None（调用方应继续等待）。"""
    status = str(status or "").upper()
    if status == "PASSED":
        return EXIT_PASSED
    if status in {"FAILED", "PARTIAL", "CANCELLED"}:
        return EXIT_TEST_FAILURE
    if status == "ERROR":
        return EXIT_SYSTEM_ERROR
    return None


def is_finished(status: str) -> bool:
    return str(status or "").upper() in FINISHED_RUN_STATUSES


def ci_provider(trigger_source: str) -> str:
    """触发来源 → CI 提供方。不新增数据库字段，从 trigger_source 推导。"""
    mapping = {"GITLAB": "gitlab", "CLI": "cli", "CI": "ci", "MANUAL": "manual", "SCHEDULE": "schedule"}
    return mapping.get(str(trigger_source or "").upper(), "")


# ------------------------------------------------------------------- 参数解析


class CiRequestError(Exception):
    """CI 请求层面的错误。服务层不依赖 FastAPI，由 API 层转成 HTTP 状态码。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def resolve_project(db: Session, ref: str | None) -> Project:
    value = str(ref or "").strip()
    if not value:
        raise CiRequestError(400, "缺少 project（可传项目 ID 或名称）")
    project = db.get(Project, value)
    if project is None:
        project = db.execute(select(Project).where(Project.name == value)).scalars().first()
    if project is None:
        raise CiRequestError(404, f"项目不存在：{value}")
    return project


def resolve_plan(db: Session, project_id: str, ref: str | None) -> RegressionPlan:
    value = str(ref or "").strip()
    if not value:
        raise CiRequestError(400, "缺少 plan（可传计划 ID 或名称）")
    plan = db.get(RegressionPlan, value)
    if plan is not None and plan.project_id != project_id:
        raise CiRequestError(404, f"回归计划不属于该项目：{value}")
    if plan is None:
        plan = db.execute(
            select(RegressionPlan).where(RegressionPlan.project_id == project_id, RegressionPlan.name == value)
        ).scalars().first()
    if plan is None:
        raise CiRequestError(404, f"回归计划不存在：{value}")
    if not plan.enabled:
        raise CiRequestError(400, f"回归计划已禁用，拒绝执行：{plan.name}")
    return plan


def resolve_plan_any_project(db: Session, ref: str | None) -> RegressionPlan:
    """不知道项目时按计划名/ID 全局解析。

    CI 里最自然的写法是 `--plan smoke --env test`，而 Job 未必知道项目 ID。
    所以允许省略项目，但**只在能唯一定位时才放行**：
    同名计划有两个就报 400 并列出候选，宁可让调用方补一个 --project，
    也不猜一个可能跑错项目的计划。
    """
    value = str(ref or "").strip()
    if not value:
        raise CiRequestError(400, "缺少 plan（可传计划 ID 或名称）")
    plan = db.get(RegressionPlan, value)
    if plan is not None:
        if not plan.enabled:
            raise CiRequestError(400, f"回归计划已禁用，拒绝执行：{plan.name}")
        return plan

    matches = db.execute(select(RegressionPlan).where(RegressionPlan.name == value)).scalars().all()
    if not matches:
        raise CiRequestError(404, f"回归计划不存在：{value}")
    if len(matches) > 1:
        names = []
        for item in matches:
            owner = db.get(Project, item.project_id)
            names.append(f"{owner.name if owner else item.project_id}/{item.name}")
        raise CiRequestError(400, f"存在多个同名计划，请用 --project 指明项目：{', '.join(names)}")
    plan = matches[0]
    if not plan.enabled:
        raise CiRequestError(400, f"回归计划已禁用，拒绝执行：{plan.name}")
    return plan


def resolve_environment(db: Session, project_id: str, ref: str | None) -> Environment:
    value = str(ref or "").strip()
    if not value:
        raise CiRequestError(400, "缺少 environment（可传环境 ID 或名称）")
    environment = db.get(Environment, value)
    if environment is not None and environment.project_id != project_id:
        raise CiRequestError(404, f"环境不属于该项目：{value}")
    if environment is None:
        environment = db.execute(
            select(Environment).where(Environment.project_id == project_id, Environment.name == value)
        ).scalars().first()
    if environment is None:
        raise CiRequestError(404, f"环境不存在：{value}")
    return environment


def preflight_environment(environment: Environment, base_url_override: str = "") -> list[str]:
    """在建 Run 之前先跑一遍环境体检，返回 block 级原因。

    用的是执行器同一套 `validate_environment`，不是另写一份规则 ——
    提前跑只是为了避免「明知会被门禁拦下，还先建一条 Run 再让它 ERROR」，
    让 CI 拿到的是一个明确的 4xx 而不是一条莫名其妙的失败记录。
    """
    from ..execution.security import validate_environment  # noqa: PLC0415 - 避免顶层循环依赖

    payload = {
        "name": environment.name,
        "base_url": base_url_override or environment.base_url or "",
        "environment_type": environment.environment_type or "TEST",
        "allowed_hosts": list(environment.allowed_hosts or []),
        "read_only": bool(environment.read_only),
    }
    return [item["message"] for item in validate_environment(payload) if item.get("severity") == "block"]


# --------------------------------------------------------------------- 幂等


def find_ci_run(db: Session, *, project_id: str, plan_id: str, pipeline_id: str) -> ExecutionRun | None:
    if not pipeline_id:
        return None
    return db.execute(
        select(ExecutionRun)
        .where(
            ExecutionRun.project_id == project_id,
            ExecutionRun.plan_id == plan_id,
            ExecutionRun.pipeline_id == pipeline_id,
            ExecutionRun.trigger_source.in_(list(CI_TRIGGER_SOURCES)),
        )
        .order_by(ExecutionRun.created_at.asc())
    ).scalars().first()


def create_ci_run(
    db: Session,
    *,
    project: Project,
    plan: RegressionPlan,
    environment: Environment | None,
    trigger_source: str,
    pipeline_id: str = "",
    pipeline_url: str = "",
    commit_sha: str = "",
    branch: str = "",
    base_url: str = "",
) -> tuple[ExecutionRun, bool]:
    """幂等创建 Run。返回 (run, 是否本次新建)。

    两层保护：先查一次（省掉大部分重复请求），再靠唯一索引兜住并发。
    捕获 IntegrityError 后必须 rollback —— 失败的事务在 SQLAlchemy 里是「中毒」状态，
    不 rollback 后续任何查询都会继续抛异常。
    """
    pipeline_id = str(pipeline_id or "").strip()
    if pipeline_id:
        existing = find_ci_run(db, project_id=project.id, plan_id=plan.id, pipeline_id=pipeline_id)
        if existing is not None:
            logger.info("CI 幂等命中：pipeline=%s plan=%s run=%s", pipeline_id, plan.id, existing.id)
            return existing, False

    try:
        run = create_run(
            db,
            project_id=project.id,
            plan_id=plan.id,
            environment_id=environment.id if environment else None,
            trigger_source=trigger_source,
            commit_sha=str(commit_sha or "")[:64],
            branch=str(branch or "")[:128],
            pipeline_id=pipeline_id[:64],
            pipeline_url=redact_text(str(pipeline_url or ""))[:255],
            base_url_override=redact_text(str(base_url or ""))[:255],
        )
    except IntegrityError:
        db.rollback()
        existing = find_ci_run(db, project_id=project.id, plan_id=plan.id, pipeline_id=pipeline_id)
        if existing is None:  # pragma: no cover - 唯一索引冲突却查不到，说明不是幂等冲突
            raise CiRequestError(409, "创建执行失败：唯一约束冲突") from None
        logger.info("CI 幂等命中（并发）：pipeline=%s run=%s", pipeline_id, existing.id)
        return existing, False

    logger.info("CI 创建执行：run=%s trigger=%s pipeline=%s", run.id, trigger_source, pipeline_id or "-")
    return run, True


# ------------------------------------------------------------------- 结果视图


def failure_group_summary(db: Session, run_id: str) -> dict[str, Any]:
    """这次 Run 聚合出的失败分组：总数 + 各类数量。

    直接读 M3 已经算好的 Group 与 Occurrence，不重算、不重新分类 ——
    CI 看到的结论与失败中心页面上看到的必须是同一份数据。
    """
    rows = db.execute(
        select(FailureGroup.category, func.count(distinct(FailureGroup.id)))
        .join(FailureOccurrence, FailureOccurrence.failure_group_id == FailureGroup.id)
        .where(FailureOccurrence.run_id == run_id)
        .group_by(FailureGroup.category)
    ).all()
    categories = {str(row[0]): int(row[1]) for row in rows}
    return {"total": sum(categories.values()), "categories": categories}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def ci_status_payload(db: Session, run: ExecutionRun) -> dict[str, Any]:
    """CI 视角的 Run 视图。JSON schema 稳定，字段只增不改。

    这是 CLI、GitLab、以及任何外部系统了解一次执行的**唯一**入口 ——
    三种调用方拿到的形状完全一致，不会出现「CLI 看到的和 webhook 看到的不一样」。
    """
    plan = db.get(RegressionPlan, run.plan_id) if run.plan_id else None
    environment = db.get(Environment, run.environment_id) if run.environment_id else None
    status = str(run.status or "").upper()
    exit_code = run_exit_code(status)

    return {
        "run_id": run.id,
        "status": status,
        "finished": is_finished(status),
        "exit_code": exit_code,
        "exit_code_meaning": _exit_meaning(exit_code),
        "project_id": run.project_id,
        "plan_id": run.plan_id or "",
        "plan_name": plan.name if plan else "",
        "environment_id": run.environment_id or "",
        "environment": environment.name if environment else "",
        "trigger_source": run.trigger_source or "",
        "ci_provider": ci_provider(run.trigger_source or ""),
        "pipeline_id": run.pipeline_id or "",
        "pipeline_url": redact_text(run.pipeline_url or ""),
        "commit_sha": run.commit_sha or "",
        "branch": run.branch or "",
        "summary": {
            "total": int(run.total_cases or 0),
            "passed": int(run.passed_cases or 0),
            "failed": int(run.failed_cases or 0),
            "skipped": int(run.skipped_cases or 0),
            "cancelled": int(run.cancelled_cases or 0),
            "error": int(run.error_cases or 0),
            "flaky": int(run.flaky_cases or 0),
        },
        "failure_groups": failure_group_summary(db, run.id),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_ms": int(run.duration_ms or 0),
        # 已经是脱敏后的文本（落库时就过了一次），这里再兜一次防止老数据漏网
        "error_message": redact_text(run.error_message or "")[:500],
    }


def _exit_meaning(exit_code: int | None) -> str:
    if exit_code == EXIT_PASSED:
        return "TestFlowCI 完成执行，全部用例通过"
    if exit_code == EXIT_TEST_FAILURE:
        return "TestFlowCI 完成执行，但存在未通过的用例（含被测环境问题）"
    if exit_code == EXIT_SYSTEM_ERROR:
        return "TestFlowCI 自身未能完成可靠执行（平台侧问题）"
    return "执行尚未结束"


__all__ = [
    "EXIT_PASSED",
    "EXIT_TEST_FAILURE",
    "EXIT_SYSTEM_ERROR",
    "FINISHED_RUN_STATUSES",
    "CiRequestError",
    "ci_provider",
    "ci_status_payload",
    "create_ci_run",
    "failure_group_summary",
    "find_ci_run",
    "is_finished",
    "preflight_environment",
    "resolve_environment",
    "resolve_plan",
    "resolve_plan_any_project",
    "resolve_project",
    "run_exit_code",
]
