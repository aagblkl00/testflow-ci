from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..auth.deps import get_current_user, require_writer
from ..config import settings
from ..db import get_db
from ..execution.service import create_run, failed_case_ids, request_cancel
from ..models import (
    FAILURE_CATEGORIES,
    Artifact,
    CaseResult,
    CaseResultAttempt,
    Environment,
    ExecutionRun,
    FailureFingerprint,
    FailureGroup,
    RegressionPlan,
    StepResult,
    User,
)
from ..schemas import ArtifactOut, AttemptOut, CancelRequest, CaseResultOut, RunCreate, RunOut, StepResultOut
from ..utils.artifact_signing import build_artifact_url, verify_artifact_access
from ..workers import dispatch_run

router = APIRouter(prefix="/api", tags=["runs"])

CASE_EAGER = (
    selectinload(CaseResult.attempts).selectinload(CaseResultAttempt.step_results),
    selectinload(CaseResult.step_results),
)


def _serialize_case(case: CaseResult) -> CaseResultOut:
    """把 CaseResult 展开成 Run → Case → Attempt → Step 的形状。

    `attempts` 是 M2 的主结构；`step_results` 保留为所有 attempt 的扁平汇总，
    这样 M1 的页面与验收脚本不用改也能继续工作。
    """
    payload = CaseResultOut.model_validate(case)
    attempts: list[AttemptOut] = []
    flat: list[StepResultOut] = []
    for attempt in sorted(case.attempts, key=lambda item: item.attempt_no):
        steps = [StepResultOut.model_validate(item) for item in sorted(attempt.step_results, key=lambda s: s.step_no)]
        item = AttemptOut.model_validate(attempt)
        item.step_results = steps
        attempts.append(item)
        flat.extend(steps)
    if not flat:
        flat = [StepResultOut.model_validate(item) for item in sorted(case.step_results, key=lambda s: s.step_no)]
    payload.attempts = attempts
    payload.step_results = flat
    return payload


def _artifact_disposition(artifact_type: str) -> str:
    """决定文件是浏览器内联打开还是作为附件下载。"""
    return "attachment" if str(artifact_type or "").lower() in {"trace", "har"} else "inline"


def artifact_url(artifact_id: str, artifact_type: str = "log") -> str:
    """把 Artifact ID 转成带短时签名的 content URL。

    数据库里的 Artifact.path 是内部存储信息，不再直接暴露成 `/storage/...`。
    """
    if not artifact_id:
        return ""
    return build_artifact_url(artifact_id, _artifact_disposition(artifact_type))


def _safe_artifact_path(raw_path: str) -> Path:
    """把数据库里的内部路径解析并限制在 storage 目录内。"""
    if not raw_path:
        raise HTTPException(status_code=403, detail="该文件不允许访问")
    try:
        path = Path(raw_path).resolve()
        path.relative_to(settings.storage_dir.resolve())
    except (ValueError, OSError):
        raise HTTPException(status_code=403, detail="该文件不允许访问") from None
    return path


def _artifact_media_type(artifact: Artifact, path: Path) -> str:
    known = {
        "screenshot": "image/png",
        "video": "video/webm",
        "trace": "application/zip",
        "har": "application/json",
        "console": "text/plain; charset=utf-8",
        "network": "text/plain; charset=utf-8",
        "log": "text/plain; charset=utf-8",
        "dom": "text/html; charset=utf-8",
    }
    if artifact.type in known:
        return known[artifact.type]
    guessed = mimetypes.guess_type(path.name)[0]
    return guessed or "application/octet-stream"


def _serialize_run(run: ExecutionRun, plan: RegressionPlan | None, environment: Environment | None) -> RunOut:
    payload = RunOut.model_validate(run)
    payload.plan_name = plan.name if plan else ""
    payload.environment_name = environment.name if environment else ""
    # 防御性：历史行可能因为迁移顺序出现 NULL，这里统一成空串，
    # 不指望数据层一定干净。
    payload.rerun_mode = run.rerun_mode or ""
    return payload


@router.post("/runs", response_model=RunOut, status_code=201)
def start_run(payload: RunCreate, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> RunOut:
    plan = db.get(RegressionPlan, payload.plan_id) if payload.plan_id else None
    if payload.plan_id and plan is None:
        raise HTTPException(status_code=404, detail="回归计划不存在")
    project_id = payload.project_id or (plan.project_id if plan else "")
    if not project_id:
        raise HTTPException(status_code=400, detail="必须提供 plan_id 或 project_id")
    if not (payload.environment_id or (plan.environment_id if plan else None)):
        raise HTTPException(status_code=400, detail="回归计划未绑定执行环境，无法启动浏览器")

    run = create_run(
        db,
        project_id=project_id,
        plan_id=payload.plan_id,
        environment_id=payload.environment_id,
        trigger_source=payload.trigger_source or "MANUAL",
        commit_sha=payload.commit_sha,
        branch=payload.branch,
        pipeline_id=payload.pipeline_id,
        pipeline_url=payload.pipeline_url,
        case_ids=payload.case_ids,
    )
    dispatch_run(run.id, blocking=payload.blocking)
    db.refresh(run)
    environment = db.get(Environment, run.environment_id) if run.environment_id else None
    return _serialize_run(run, plan, environment)


@router.post("/runs/{run_id}/stop", response_model=RunOut)
def stop_run(
    run_id: str,
    payload: CancelRequest | None = None,
    user: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> RunOut:
    """停止执行。

    这里不只是改数据库状态：request_cancel 会同时置位进程内停止信号，
    执行器在下一次检查点（步骤边界 / wait 切片）就会退出并关闭浏览器。
    Celery 模式下则由执行器轮询 cancel_requested 字段感知。
    """
    try:
        run = request_cancel(db, run_id, actor=user.username)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    plan = db.get(RegressionPlan, run.plan_id) if run.plan_id else None
    environment = db.get(Environment, run.environment_id) if run.environment_id else None
    _ = payload  # 预留停止原因，M2 不落库
    return _serialize_run(run, plan, environment)


@router.post("/runs/{run_id}/rerun-failed", response_model=RunOut, status_code=201)
def rerun_failed(run_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> RunOut:
    """只重跑原 Run 里失败的用例。

    新 Run 独立保存（source_run_id 指回原 Run），原 Run 的记录一个字节都不改。
    """
    source = db.get(ExecutionRun, run_id)
    if source is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    if source.status in {"QUEUED", "RUNNING", "CANCELLING"}:
        raise HTTPException(status_code=409, detail="该执行尚未结束，无法重跑失败用例")
    case_ids = failed_case_ids(db, run_id)
    if not case_ids:
        raise HTTPException(status_code=400, detail="该执行没有失败用例，无需重跑")
    run = create_run(
        db,
        project_id=source.project_id,
        plan_id=source.plan_id,
        environment_id=source.environment_id,
        trigger_source=source.trigger_source or "MANUAL",
        commit_sha=source.commit_sha,
        branch=source.branch,
        pipeline_id=source.pipeline_id,
        pipeline_url=source.pipeline_url,
        case_ids=case_ids,
        source_run_id=source.id,
        rerun_mode="FAILED_ONLY",
    )
    dispatch_run(run.id, blocking=False)
    db.refresh(run)
    plan = db.get(RegressionPlan, run.plan_id) if run.plan_id else None
    environment = db.get(Environment, run.environment_id) if run.environment_id else None
    return _serialize_run(run, plan, environment)


@router.get("/runs/{run_id}/failed-cases")
def list_failed_cases(run_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    if db.get(ExecutionRun, run_id) is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    ids = failed_case_ids(db, run_id)
    return {"run_id": run_id, "failed_case_ids": ids, "count": len(ids)}


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    project_id: str | None = None,
    status: str = "",
    q: str = "",
    limit: int = Query(default=50, ge=1, le=500),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RunOut]:
    query = select(ExecutionRun).order_by(ExecutionRun.created_at.desc()).limit(limit)
    if project_id:
        query = query.where(ExecutionRun.project_id == project_id)
    if status:
        query = query.where(ExecutionRun.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            ExecutionRun.id.ilike(like) | ExecutionRun.commit_sha.ilike(like) | ExecutionRun.pipeline_id.ilike(like)
        )
    rows = db.execute(query).scalars().all()
    plan_map = {item.id: item for item in db.execute(select(RegressionPlan)).scalars().all()}
    env_map = {item.id: item for item in db.execute(select(Environment)).scalars().all()}
    return [_serialize_run(item, plan_map.get(item.plan_id or ""), env_map.get(item.environment_id or "")) for item in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> RunOut:
    run = db.get(ExecutionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    plan = db.get(RegressionPlan, run.plan_id) if run.plan_id else None
    environment = db.get(Environment, run.environment_id) if run.environment_id else None
    return _serialize_run(run, plan, environment)


@router.get("/runs/{run_id}/cases", response_model=list[CaseResultOut])
def list_run_cases(run_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[CaseResultOut]:
    if db.get(ExecutionRun, run_id) is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    rows = db.execute(
        select(CaseResult).options(*CASE_EAGER).where(CaseResult.run_id == run_id).order_by(CaseResult.started_at)
    ).scalars().all()
    return [_serialize_case(item) for item in rows]


@router.get("/case-results/{case_result_id}", response_model=CaseResultOut)
def get_case_result(case_result_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> CaseResultOut:
    row = db.execute(
        select(CaseResult).options(*CASE_EAGER).where(CaseResult.id == case_result_id)
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="用例执行结果不存在")
    return _serialize_case(row)


@router.get("/case-results/{case_result_id}/attempts", response_model=list[AttemptOut])
def list_attempts(case_result_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[AttemptOut]:
    """每一次真实重新执行都独立可查 —— Flaky 的判定依据就在这里。"""
    row = db.execute(
        select(CaseResult).options(*CASE_EAGER).where(CaseResult.id == case_result_id)
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="用例执行结果不存在")
    return _serialize_case(row).attempts


@router.get("/attempts/{attempt_id}", response_model=AttemptOut)
def get_attempt(attempt_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AttemptOut:
    row = db.execute(select(CaseResultAttempt).where(CaseResultAttempt.id == attempt_id)).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="执行尝试不存在")
    payload = AttemptOut.model_validate(row)
    payload.step_results = [StepResultOut.model_validate(item) for item in sorted(row.step_results, key=lambda s: s.step_no)]
    return payload


@router.get("/attempts/{attempt_id}/artifacts", response_model=list[ArtifactOut])
def list_attempt_artifacts(attempt_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ArtifactOut]:
    rows = db.execute(
        select(Artifact).where(Artifact.attempt_id == attempt_id).order_by(Artifact.created_at)
    ).scalars().all()
    result: list[ArtifactOut] = []
    for row in rows:
        payload = ArtifactOut.model_validate(row)
        payload.url = artifact_url(row.id, row.type)
        result.append(payload)
    return result


@router.get("/step-results/{step_result_id}", response_model=StepResultOut)
def get_step_result(step_result_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> StepResultOut:
    row = db.get(StepResult, step_result_id)
    if row is None:
        raise HTTPException(status_code=404, detail="步骤结果不存在")
    return StepResultOut.model_validate(row)


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactOut])
def list_run_artifacts(run_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ArtifactOut]:
    rows = db.execute(
        select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
    ).scalars().all()
    result: list[ArtifactOut] = []
    for row in rows:
        payload = ArtifactOut.model_validate(row)
        payload.url = artifact_url(row.id, row.type)
        result.append(payload)
    return result


@router.get("/case-results/{case_result_id}/artifacts", response_model=list[ArtifactOut])
def list_case_artifacts(case_result_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[ArtifactOut]:
    rows = db.execute(
        select(Artifact).where(Artifact.case_result_id == case_result_id).order_by(Artifact.created_at)
    ).scalars().all()
    result: list[ArtifactOut] = []
    for row in rows:
        payload = ArtifactOut.model_validate(row)
        payload.url = artifact_url(row.id, row.type)
        result.append(payload)
    return result


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
def get_artifact(artifact_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ArtifactOut:
    row = db.get(Artifact, artifact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="证据文件不存在")
    payload = ArtifactOut.model_validate(row)
    payload.url = artifact_url(row.id, row.type)
    return payload


@router.get("/artifacts/{artifact_id}/content")
def read_artifact_content(
    artifact_id: str,
    disposition: str = Query(default="inline", pattern="^(inline|attachment)$"),
    exp: str | None = Query(default=None),
    sig: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> FileResponse:
    """返回 Artifact 文件内容。

    两种合法访问方式：
    1. 带 `Authorization: Bearer <token>`，走现有用户认证；
    2. 带短时 HMAC 签名 `exp` / `sig`，用于 `<img>` / `<video>` / `<a href>`。
    两种方式都只是过了「入口关」；之后仍按 Artifact ID 查库、校验 path。
    """
    if exp or sig:
        if not verify_artifact_access(artifact_id, disposition, exp, sig):
            raise HTTPException(status_code=403, detail="Artifact 访问签名无效或已过期")
    else:
        get_current_user(authorization, db)

    artifact = db.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="证据文件不存在")

    path = _safe_artifact_path(artifact.path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=410, detail="证据文件已不在磁盘上")

    return FileResponse(
        path,
        media_type=_artifact_media_type(artifact, path),
        filename=path.name,
        content_disposition_type=disposition,
    )


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> FileResponse:
    row = db.get(Artifact, artifact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="证据文件不存在")
    path = _safe_artifact_path(row.path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="证据文件已不在磁盘上")
    return FileResponse(path, media_type=_artifact_media_type(row, path), filename=path.name)


@router.post("/runs/{run_id}/rerun", response_model=RunOut, status_code=201)
def rerun(run_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> RunOut:
    source = db.get(ExecutionRun, run_id)
    if source is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    if source.status in {"QUEUED", "RUNNING"}:
        raise HTTPException(status_code=409, detail="该执行仍在进行中，无法重跑")
    run = create_run(
        db,
        project_id=source.project_id,
        plan_id=source.plan_id,
        environment_id=source.environment_id,
        trigger_source=source.trigger_source or "MANUAL",
        commit_sha=source.commit_sha,
        branch=source.branch,
        pipeline_id=source.pipeline_id,
        pipeline_url=source.pipeline_url,
    )
    dispatch_run(run.id, blocking=False)
    db.refresh(run)
    plan = db.get(RegressionPlan, run.plan_id) if run.plan_id else None
    environment = db.get(Environment, run.environment_id) if run.environment_id else None
    return _serialize_run(run, plan, environment)


@router.get("/projects/{project_id}/dashboard/summary")
def project_dashboard(project_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    runs = db.execute(
        select(ExecutionRun).where(ExecutionRun.project_id == project_id).order_by(ExecutionRun.created_at.desc()).limit(20)
    ).scalars().all()
    totals = db.execute(
        select(
            func.count(ExecutionRun.id),
            func.coalesce(func.sum(ExecutionRun.passed_cases), 0),
            func.coalesce(func.sum(ExecutionRun.failed_cases), 0),
            func.coalesce(func.sum(ExecutionRun.skipped_cases), 0),
            func.coalesce(func.sum(ExecutionRun.flaky_cases), 0),
        ).where(ExecutionRun.project_id == project_id)
    ).one()
    total_runs, passed, failed, skipped, flaky = (int(totals[index] or 0) for index in range(5))
    executed = passed + failed + flaky

    # ---- M3：失败中心的聚合视图。全部读 FailureGroup 上已经算好的列，
    # 不做实时重算，所以页面打开成本与历史规模无关。
    failure_groups = db.execute(
        select(FailureGroup).where(FailureGroup.project_id == project_id).order_by(FailureGroup.last_seen_at.desc())
    ).scalars().all()
    category_counts = {name: 0 for name in FAILURE_CATEGORIES}
    for group in failure_groups:
        category_counts[group.category] = category_counts.get(group.category, 0) + 1

    open_groups = [item for item in failure_groups if item.status == "OPEN"]
    fingerprints = {
        item.id: item
        for item in db.execute(
            select(FailureFingerprint).where(
                FailureFingerprint.id.in_({group.fingerprint_id for group in open_groups[:5]} or {""})
            )
        ).scalars().all()
    }
    recent_groups = []
    for group in open_groups[:5]:
        fingerprint = fingerprints.get(group.fingerprint_id)
        recent_groups.append(
            {
                "id": group.id,
                "title": group.title,
                "category": group.category,
                "rule_id": group.rule_id,
                "status": group.status,
                "occurrence_count": group.occurrence_count,
                "failed_case_count": group.failed_case_count,
                "failed_run_count": group.failed_run_count,
                "flaky_count": group.flaky_count,
                "last_seen_at": group.last_seen_at.isoformat() if group.last_seen_at else None,
                "latest_run_id": group.latest_run_id,
                "fingerprint": fingerprint.fingerprint if fingerprint else "",
                "strategy": fingerprint.strategy if fingerprint else "",
            }
        )

    case_executions = int(
        db.execute(
            select(func.count(CaseResult.id))
            .join(ExecutionRun, ExecutionRun.id == CaseResult.run_id)
            .where(ExecutionRun.project_id == project_id)
        ).scalar()
        or 0
    )
    flaky_case_executions = int(
        db.execute(
            select(func.count(CaseResult.id))
            .join(ExecutionRun, ExecutionRun.id == CaseResult.run_id)
            .where(ExecutionRun.project_id == project_id, CaseResult.status == "FLAKY")
        ).scalar()
        or 0
    )

    return {
        "total_runs": total_runs,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "flaky": flaky,
        "pass_rate": round((passed + flaky) / executed * 100, 1) if executed else 0.0,
        "running": db.execute(
            select(func.count(ExecutionRun.id)).where(
                ExecutionRun.project_id == project_id, ExecutionRun.status.in_(["QUEUED", "RUNNING"])
            )
        ).scalar_one(),
        "recent_runs": [_serialize_run(item, None, None).model_dump() for item in runs],
        # M3 失败中心
        "failure_groups": len(failure_groups),
        "open_failure_groups": len(open_groups),
        "failure_categories": category_counts,
        "recent_failure_groups": recent_groups,
        "case_executions": case_executions,
        "flaky_case_executions": flaky_case_executions,
        "flaky_rate": round(flaky_case_executions / case_executions * 100, 1) if case_executions else 0.0,
    }
