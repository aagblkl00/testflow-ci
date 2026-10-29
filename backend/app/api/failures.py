"""M3 失败中心 API。

对外只暴露「问题」这个粒度（FailureGroup），而不是几百条 CaseResult ——
这是整个 M3 的价值所在：测试人员一屏看到的是 5 个问题，不是 38 条失败。

所有列表接口**从第一版就分页**。方案 §54 说得很明确：不能每打开一次页面
就扫描全部历史 Run 并重算指纹。这里的过滤条件全部下推到 SQL，
Group 上的计数是聚合时写好的，不做实时全表运算。

证据系统不在这里复制一份：钻取链路是
Group → Occurrence → CaseResult → Attempt → StepResult → Artifact，
Artifact 直接复用 M2 的 `/api/artifacts/{id}` 与 `/api/runs/{id}/artifacts`。
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, distinct, func, or_, select
from sqlalchemy.orm import Session

from ..auth.deps import get_current_user, require_writer
from ..db import get_db
from ..failure import classifier as classifier_module
from ..failure import retry_policy
from ..failure.service import analyze_run
from ..models import (
    ARTIFACT_TYPES,
    FAILURE_CATEGORIES,
    CaseResult,
    CaseResultAttempt,
    Environment,
    ExecutionRun,
    FailureFingerprint,
    FailureGroup,
    FailureOccurrence,
    Project,
    RegressionPlan,
    StepResult,
    User,
)
from ..schemas import (
    FailureAnalyzeResult,
    FailureFingerprintOut,
    FailureGroupOut,
    FailureGroupPage,
    FailureGroupPatch,
    FailureOccurrenceOut,
    FailureOccurrencePage,
    FailureTrendOut,
    FailureTrendPoint,
    FlakyCaseStat,
    FlakyGroupStat,
    FlakyRunStat,
    FlakyStatistics,
    RetryPolicyOut,
)

router = APIRouter(prefix="/api", tags=["failures"])

MAX_PAGE_SIZE = 200


# ----------------------------------------------------------------- 查询条件构造


def _parse_day(value: str | None, *, end: bool = False) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:  # YYYY-MM-DD
            day = datetime.strptime(text, "%Y-%m-%d").date()
            bound = time.max if end else time.min
            return datetime.combine(day, bound, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"时间格式不正确：{value}（应为 YYYY-MM-DD 或 ISO 时间）") from None


def _group_filter_query(
    *,
    project_id: str | None,
    category: str | None,
    status: str | None,
    run_id: str | None,
    case_id: str | None,
    date_from: str | None,
    date_to: str | None,
    q: str | None,
):
    """把筛选条件下推到 SQL。

    涉及 Occurrence 的条件（run_id / case_id）用 exists 子查询而不是 join：
    join 会让同一个 Group 因为多条 occurrence 而重复出现，还得再 distinct，
    计数也跟着失真。
    """
    query = select(FailureGroup)
    if project_id:
        query = query.where(FailureGroup.project_id == project_id)
    if category:
        if category.upper() not in FAILURE_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"category 必须是 {FAILURE_CATEGORIES} 之一")
        query = query.where(FailureGroup.category == category.upper())
    if status:
        query = query.where(FailureGroup.status == status.upper())
    if run_id:
        query = query.where(
            select(FailureOccurrence.id)
            .where(FailureOccurrence.failure_group_id == FailureGroup.id, FailureOccurrence.run_id == run_id)
            .exists()
        )
    if case_id:
        query = query.where(
            select(FailureOccurrence.id)
            .join(CaseResult, CaseResult.id == FailureOccurrence.case_result_id)
            .where(FailureOccurrence.failure_group_id == FailureGroup.id, CaseResult.case_id == case_id)
            .exists()
        )
    start = _parse_day(date_from)
    end = _parse_day(date_to, end=True)
    if start is not None:
        query = query.where(FailureGroup.last_seen_at >= start)
    if end is not None:
        query = query.where(FailureGroup.last_seen_at <= end)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                FailureGroup.title.ilike(like),
                FailureGroup.rule_id.ilike(like),
                FailureGroup.rule_reason.ilike(like),
                FailureGroup.id.ilike(like),
            )
        )
    return query


def _paginate(query, db: Session, *, page: int, page_size: int) -> tuple[list[Any], int, int]:
    page = max(1, int(page))
    page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
    total = int(
        db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar() or 0
    )
    pages = (total + page_size - 1) // page_size
    rows = db.execute(query.limit(page_size).offset((page - 1) * page_size)).scalars().all()
    return list(rows), total, pages


# --------------------------------------------------------------------- 序列化


def _retry_hint(category: str) -> dict[str, Any]:
    """这个分类在「系统自动重试」模式下会被怎么处理。"""
    return retry_policy.explain(0, category, auto_retry=True)


def _serialize_group(
    group: FailureGroup,
    *,
    fingerprint: FailureFingerprint | None = None,
    project_name: str = "",
    with_fingerprint: bool = True,
) -> FailureGroupOut:
    payload = FailureGroupOut.model_validate(group)
    payload.project_name = project_name
    payload.retry_policy = _retry_hint(group.category)
    if with_fingerprint and fingerprint is not None:
        payload.fingerprint = FailureFingerprintOut.model_validate(fingerprint)
    return payload


def _load_fingerprints(db: Session, group_rows: list[FailureGroup]) -> dict[str, FailureFingerprint]:
    ids = {item.fingerprint_id for item in group_rows}
    if not ids:
        return {}
    rows = db.execute(select(FailureFingerprint).where(FailureFingerprint.id.in_(ids))).scalars().all()
    return {item.id: item for item in rows}


# ------------------------------------------------------------------------ 列表


@router.get("/failures", response_model=FailureGroupPage)
def list_failures(
    project_id: str | None = None,
    category: str | None = None,
    status: str | None = None,
    run_id: str | None = None,
    case_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FailureGroupPage:
    """Failure Group 列表。默认按最近发生时间倒序 —— 最近还在发生的问题排最前。"""
    filters = {
        "project_id": project_id,
        "category": category,
        "status": status,
        "run_id": run_id,
        "case_id": case_id,
        "date_from": date_from,
        "date_to": date_to,
        "q": q,
    }
    query = _group_filter_query(**filters).order_by(FailureGroup.last_seen_at.desc(), FailureGroup.id.desc())
    rows, total, pages = _paginate(query, db, page=page, page_size=page_size)

    # 分类分布刻意忽略 category 条件：否则切到某一类之后，其他类的数字全变成 0，
    # Tab 就失去了导航作用。
    count_query = _group_filter_query(**{**filters, "category": None})
    counts = {
        str(row[0]): int(row[1])
        for row in db.execute(
            count_query.with_only_columns(FailureGroup.category, func.count(FailureGroup.id)).group_by(
                FailureGroup.category
            )
        ).all()
    }

    fingerprints = _load_fingerprints(db, rows)
    projects = {
        item.id: item.name for item in db.execute(select(Project)).scalars().all()
    }
    items = [
        _serialize_group(
            item,
            fingerprint=fingerprints.get(item.fingerprint_id),
            project_name=projects.get(item.project_id, ""),
        )
        for item in rows
    ]
    return FailureGroupPage(
        items=items,
        total=total,
        page=max(1, page),
        page_size=page_size,
        pages=pages,
        category_counts=counts,
    )


@router.get("/failures/stats")
def failure_stats(
    project_id: str | None = None,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """给 Dashboard 用的轻量统计。全部走 Group 上已经算好的列，不做实时聚合。"""
    base = select(FailureGroup)
    if project_id:
        base = base.where(FailureGroup.project_id == project_id)
    subquery = base.with_only_columns(FailureGroup.category, FailureGroup.status).subquery()
    by_category = {
        str(row[0]): int(row[1])
        for row in db.execute(
            select(subquery.c.category, func.count()).group_by(subquery.c.category)
        ).all()
    }
    totals = db.execute(
        select(
            func.count(subquery.c.category),
            func.sum(case((subquery.c.status == "OPEN", 1), else_=0)),
        )
    ).one()
    categories = {name: by_category.get(name, 0) for name in FAILURE_CATEGORIES}
    return {
        "total_groups": int(totals[0] or 0),
        "open_groups": int(totals[1] or 0),
        "categories": categories,
    }


@router.get("/fingerprints", response_model=dict)
def list_fingerprints(
    strategy: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(FailureFingerprint)
    if strategy:
        query = query.where(FailureFingerprint.strategy == strategy.upper())
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                FailureFingerprint.fingerprint.ilike(like),
                FailureFingerprint.normalized_message.ilike(like),
                FailureFingerprint.request_url.ilike(like),
                FailureFingerprint.locator.ilike(like),
            )
        )
    query = query.order_by(FailureFingerprint.created_at.desc())
    rows, total, pages = _paginate(query, db, page=page, page_size=page_size)

    # 每个指纹被多少个 Group 用到（正常情况下就是 1，Cross-project 时可能 >1）
    usage = {
        str(row[0]): int(row[1])
        for row in db.execute(
            select(FailureGroup.fingerprint_id, func.count(FailureGroup.id)).group_by(FailureGroup.fingerprint_id)
        ).all()
    }
    items = []
    for row in rows:
        payload = FailureFingerprintOut.model_validate(row)
        payload.occurrence_groups = usage.get(row.id, 0)
        items.append(payload.model_dump())
    return {"items": items, "total": total, "page": max(1, page), "page_size": page_size, "pages": pages}


@router.get("/retry-policy", response_model=RetryPolicyOut)
def get_retry_policy(_: User = Depends(get_current_user)) -> RetryPolicyOut:
    """暴露当前生效的重试策略与它的来源（内置默认 / 配置文件 / 环境变量）。"""
    info = retry_policy.as_dict()
    return RetryPolicyOut(
        defaults=info["defaults"],
        effective=info["effective"],
        source=info["source"],
        precedence=[
            "1. 被安全门禁拦下 → 不重试（重试没有任何意义）",
            "2. 计划显式配置的 retry_count > 0 → 按显式值执行，不因分类被压低",
            "3. 计划开启「失败自动重试」→ 按失败分类取策略值",
            "4. 都没有 → 不重试（与 M1/M2 行为一致）",
        ],
    )


@router.get("/flaky/statistics", response_model=FlakyStatistics)
def flaky_statistics(
    project_id: str | None = None,
    case_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FlakyStatistics:
    """Flaky 统计。分母显式返回，定义写在响应里（方案 §33/§34）。"""
    case_query = select(CaseResult)
    if project_id:
        case_query = case_query.join(ExecutionRun, ExecutionRun.id == CaseResult.run_id).where(
            ExecutionRun.project_id == project_id
        )
    if case_id:
        case_query = case_query.where(CaseResult.case_id == case_id)
    total_executions = int(
        db.execute(select(func.count()).select_from(case_query.order_by(None).subquery())).scalar() or 0
    )

    flaky_query = case_query.where(CaseResult.status == "FLAKY")
    flaky_executions = int(
        db.execute(select(func.count()).select_from(flaky_query.order_by(None).subquery())).scalar() or 0
    )

    by_case_rows = db.execute(
        select(
            CaseResult.case_id,
            func.max(CaseResult.case_code),
            func.max(CaseResult.case_name),
            func.count(CaseResult.id),
            func.sum(case((CaseResult.status == "FLAKY", 1), else_=0)),
        )
        .select_from(CaseResult)
        .join(ExecutionRun, ExecutionRun.id == CaseResult.run_id)
        .where(*([] if not project_id else [ExecutionRun.project_id == project_id]))
        .group_by(CaseResult.case_id)
        .order_by(func.sum(case((CaseResult.status == "FLAKY", 1), else_=0)).desc())
        .limit(limit)
    ).all()

    by_case = [
        FlakyCaseStat(
            case_id=row[0],
            case_code=row[1] or "",
            case_name=row[2] or "",
            executions=int(row[3] or 0),
            flaky_count=int(row[4] or 0),
            flaky_rate=round(int(row[4] or 0) / int(row[3] or 1) * 100, 1),
        )
        for row in by_case_rows
        if int(row[4] or 0) > 0
    ]

    run_query = select(ExecutionRun).where(ExecutionRun.flaky_cases > 0)
    if project_id:
        run_query = run_query.where(ExecutionRun.project_id == project_id)
    run_rows = db.execute(run_query.order_by(ExecutionRun.created_at.desc()).limit(limit)).scalars().all()
    by_run = [
        FlakyRunStat(
            run_id=item.id,
            started_at=item.started_at,
            flaky_cases=int(item.flaky_cases or 0),
            total_cases=int(item.total_cases or 0),
        )
        for item in run_rows
    ]

    group_query = select(FailureGroup).where(FailureGroup.flaky_count > 0)
    if project_id:
        group_query = group_query.where(FailureGroup.project_id == project_id)
    group_rows = db.execute(group_query.order_by(FailureGroup.flaky_count.desc()).limit(limit)).scalars().all()
    flaky_groups = [
        FlakyGroupStat(
            group_id=item.id,
            title=item.title,
            category=item.category,
            flaky_occurrences=int(item.flaky_count or 0),
            all_occurrences=int(item.occurrence_count or 0),
        )
        for item in group_rows
    ]

    distinct_flaky = int(
        db.execute(
            select(func.count(distinct(CaseResult.case_id)))
            .select_from(CaseResult)
            .join(ExecutionRun, ExecutionRun.id == CaseResult.run_id)
            .where(CaseResult.status == "FLAKY", *([] if not project_id else [ExecutionRun.project_id == project_id]))
        ).scalar()
        or 0
    )

    return FlakyStatistics(
        total_case_executions=total_executions,
        flaky_case_executions=flaky_executions,
        flaky_rate=round(flaky_executions / total_executions * 100, 1) if total_executions else 0.0,
        distinct_flaky_cases=distinct_flaky,
        flaky_groups=flaky_groups,
        by_case=by_case,
        by_run=by_run,
    )


# ------------------------------------------------------------------------ 详情


@router.get("/failures/{group_id}", response_model=FailureGroupOut)
def get_failure(group_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> FailureGroupOut:
    group = db.get(FailureGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="失败分组不存在")
    fingerprint = db.get(FailureFingerprint, group.fingerprint_id)
    project = db.get(Project, group.project_id)
    return _serialize_group(group, fingerprint=fingerprint, project_name=project.name if project else "")


@router.patch("/failures/{group_id}", response_model=FailureGroupOut)
def patch_failure(
    group_id: str,
    payload: FailureGroupPatch,
    _: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> FailureGroupOut:
    """人工改状态。

    刻意不提供自动 RESOLVED：方案 §22 说得对 ——「本次没有失败」不等于
    「这个历史问题已经解决」。忽略某个分组容易，把已修复的问题误判成已解决代价很大，
    所以这一步必须由人来做。
    """
    group = db.get(FailureGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="失败分组不存在")
    group.status = payload.status
    db.commit()
    db.refresh(group)
    fingerprint = db.get(FailureFingerprint, group.fingerprint_id)
    project = db.get(Project, group.project_id)
    return _serialize_group(group, fingerprint=fingerprint, project_name=project.name if project else "")


def _require_group(db: Session, group_id: str) -> FailureGroup:
    group = db.get(FailureGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="失败分组不存在")
    return group


def _occurrence_filters(group_id: str, run_id: str | None, case_id: str | None):
    conditions = [FailureOccurrence.failure_group_id == group_id]
    if run_id:
        conditions.append(FailureOccurrence.run_id == run_id)
    if case_id:
        conditions.append(CaseResult.case_id == case_id)
    return conditions


@router.get("/failures/{group_id}/occurrences", response_model=FailureOccurrencePage)
def list_group_occurrences(
    group_id: str,
    run_id: str | None = None,
    case_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FailureOccurrencePage:
    """出现历史。每一行都能继续钻到 Run / Case / Attempt / Step。"""
    _require_group(db, group_id)
    base = (
        select(FailureOccurrence, CaseResult, ExecutionRun, CaseResultAttempt, StepResult)
        .join(CaseResult, CaseResult.id == FailureOccurrence.case_result_id)
        .join(ExecutionRun, ExecutionRun.id == FailureOccurrence.run_id)
        .outerjoin(CaseResultAttempt, CaseResultAttempt.id == FailureOccurrence.attempt_id)
        .outerjoin(StepResult, StepResult.id == FailureOccurrence.step_result_id)
        .where(*_occurrence_filters(group_id, run_id, case_id))
        .order_by(FailureOccurrence.created_at.desc(), FailureOccurrence.id.desc())
    )
    total = int(db.execute(select(func.count()).select_from(base.order_by(None).subquery())).scalar() or 0)
    pages = (total + page_size - 1) // page_size
    rows = db.execute(base.limit(page_size).offset((max(1, page) - 1) * page_size)).all()

    plan_ids = {row[2].plan_id for row in rows if row[2].plan_id}
    env_ids = {row[2].environment_id for row in rows if row[2].environment_id}
    plans = {item.id: item.name for item in db.execute(select(RegressionPlan).where(RegressionPlan.id.in_(plan_ids))).scalars().all()} if plan_ids else {}
    envs = {item.id: item.name for item in db.execute(select(Environment).where(Environment.id.in_(env_ids))).scalars().all()} if env_ids else {}

    items: list[FailureOccurrenceOut] = []
    for occurrence, case, run, attempt, step in rows:
        payload = FailureOccurrenceOut.model_validate(occurrence)
        payload.case_code = case.case_code or ""
        payload.case_name = case.case_name or ""
        payload.run_status = run.status
        payload.run_started_at = run.started_at
        # M4：CI 信息沿 Run 带出来（Failure Group 上不再重复存 commit/branch）
        payload.run_branch = run.branch or ""
        payload.run_commit_sha = run.commit_sha or ""
        payload.run_pipeline_id = run.pipeline_id or ""
        payload.run_pipeline_url = run.pipeline_url or ""
        payload.run_trigger_source = run.trigger_source or ""
        payload.plan_id = run.plan_id
        payload.plan_name = plans.get(run.plan_id or "", "")
        payload.environment_name = envs.get(run.environment_id or "", "")
        if attempt is not None:
            payload.attempt_no = int(attempt.attempt_no or 0)
            payload.attempt_status = attempt.status
        if step is not None:
            payload.step_no = int(step.step_no or 0)
            payload.step_action = step.action or ""
            payload.step_status = step.status
            payload.step_error = (step.error or "")[:600]
            payload.step_expected = (step.expected or "")[:300]
            payload.step_actual = (step.actual or "")[:300]
        items.append(payload)

    return FailureOccurrencePage(items=items, total=total, page=max(1, page), page_size=page_size, pages=pages)


@router.get("/failures/{group_id}/cases", response_model=list[FailureOccurrenceOut])
def list_group_cases(
    group_id: str,
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[FailureOccurrenceOut]:
    """受影响的用例（按用例聚合，每个用例只出现一次，带最近一次的现场）。"""
    _require_group(db, group_id)
    rows = db.execute(
        select(
            CaseResult.case_id,
            CaseResult.case_code,
            CaseResult.case_name,
            func.count(FailureOccurrence.id),
            func.min(FailureOccurrence.created_at),
            func.max(FailureOccurrence.created_at),
            func.max(FailureOccurrence.is_flaky),
        )
        .join(FailureOccurrence, FailureOccurrence.case_result_id == CaseResult.id)
        .where(FailureOccurrence.failure_group_id == group_id)
        .group_by(CaseResult.case_id, CaseResult.case_code, CaseResult.case_name)
        .order_by(func.count(FailureOccurrence.id).desc())
        .limit(limit)
    ).all()
    result: list[FailureOccurrenceOut] = []
    for case_id, code, name, count, first_seen, last_seen, is_flaky in rows:
        result.append(
            FailureOccurrenceOut(
                id="",
                failure_group_id=group_id,
                fingerprint_id="",
                run_id="",
                case_result_id=case_id or "",
                case_code=code or "",
                case_name=name or "",
                is_flaky=bool(is_flaky),
                step_no=int(count or 0),
                step_status=f"影响 {int(count or 0)} 次",
                step_error=f"首次 {first_seen} / 最近 {last_seen}",
            )
        )
    return result


@router.get("/failures/{group_id}/runs", response_model=list[FailureTrendPoint])
def list_group_runs(
    group_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[FailureTrendPoint]:
    """受影响的 Run（按 Run 聚合）。"""
    _require_group(db, group_id)
    rows = db.execute(
        select(
            FailureOccurrence.run_id,
            func.count(FailureOccurrence.id),
            func.count(distinct(FailureOccurrence.case_result_id)),
            func.sum(case((FailureOccurrence.is_flaky.is_(True), 1), else_=0)),
            ExecutionRun.status,
            ExecutionRun.started_at,
            ExecutionRun.created_at,
        )
        .join(ExecutionRun, ExecutionRun.id == FailureOccurrence.run_id)
        .where(FailureOccurrence.failure_group_id == group_id)
        .group_by(FailureOccurrence.run_id, ExecutionRun.status, ExecutionRun.started_at, ExecutionRun.created_at)
        .order_by(ExecutionRun.created_at.desc())
        .limit(limit)
    ).all()
    return [
        FailureTrendPoint(
            run_id=row[0],
            run_status=row[4] or "",
            run_started_at=row[5] or row[6],
            occurrences=int(row[1] or 0),
            cases=int(row[2] or 0),
            flaky_occurrences=int(row[3] or 0),
        )
        for row in rows
    ]


@router.get("/failures/{group_id}/trend", response_model=FailureTrendOut)
def group_trend(
    group_id: str,
    limit: int = Query(default=7, ge=1, le=60),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FailureTrendOut:
    """最近 N 个相关 Run 的出现情况。M3 只做简单趋势，不做 BI（方案 §41）。"""
    group = _require_group(db, group_id)
    points = list_group_runs(group_id, limit=limit, _=None, db=db)  # type: ignore[arg-type]
    points.reverse()  # 时间正序，方便画图
    return FailureTrendOut(
        group_id=group_id,
        points=points,
        total_occurrences=int(group.occurrence_count or 0),
        flaky_occurrences=int(group.flaky_count or 0),
    )


# ------------------------------------------------------------------ 手动触发分析


@router.post("/runs/{run_id}/analyze-failures", response_model=FailureAnalyzeResult)
def analyze_run_failures(
    run_id: str,
    _: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> FailureAnalyzeResult:
    """重新分析一次 Run。

    存在的意义有两点：一是历史 Run 可以在 M3 上线后按需补算（方案 §48 明确不允许
    偷偷补算，必须是一个显式动作）；二是让「幂等」这件事可被验证 ——
    连续调用两次，Group 与 Occurrence 数量都不能变。
    """
    run = db.get(ExecutionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="执行记录不存在")
    summary = analyze_run(db, run_id)
    return FailureAnalyzeResult(
        run_id=run_id,
        analyzed=bool(summary.get("analyzed")),
        reason=str(summary.get("reason") or ""),
        failed_cases=int(summary.get("failed_cases") or 0),
        occurrences=int(summary.get("occurrences") or 0),
        groups_touched=int(summary.get("groups_touched") or 0),
        categories=dict(summary.get("categories") or {}),
    )


@router.get("/failure-rules")
def failure_rules(_: User = Depends(get_current_user)) -> dict[str, Any]:
    """分类规则表。分类结论必须能追溯到一条具体规则，这里就是那张表。"""
    return {
        "categories": list(FAILURE_CATEGORIES),
        "rules": classifier_module.describe_rules(),
        "artifact_types": list(ARTIFACT_TYPES),
        "note": "M3 分类完全由确定性规则得出，不使用 AI；证据不足一律 UNKNOWN。",
    }
