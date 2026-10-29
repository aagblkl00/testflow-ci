from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.deps import get_current_user, require_writer
from ..db import get_db
from ..models import TRIGGER_TYPES, Environment, Project, RegressionPlan, TestSuite, TestSuiteCase, User
from ..schemas import PlanIn, PlanOut

router = APIRouter(prefix="/api", tags=["plans"])


def _validate(db: Session, payload: PlanIn) -> None:
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if payload.environment_id and db.get(Environment, payload.environment_id) is None:
        raise HTTPException(status_code=404, detail="环境不存在")
    if payload.suite_id and db.get(TestSuite, payload.suite_id) is None:
        raise HTTPException(status_code=404, detail="测试套件不存在")
    if payload.trigger_type not in TRIGGER_TYPES:
        raise HTTPException(status_code=400, detail=f"trigger_type 必须是 {TRIGGER_TYPES} 之一")
    if not payload.suite_id and not payload.case_ids:
        raise HTTPException(status_code=400, detail="必须选择测试套件，或直接指定用例")


def _enrich(db: Session, plan: RegressionPlan) -> PlanOut:
    payload = PlanOut.model_validate(plan)
    if plan.suite_id:
        suite = db.get(TestSuite, plan.suite_id)
        if suite is not None:
            payload.suite_name = suite.name
            payload.resolved_case_count = len(
                db.execute(
                    select(TestSuiteCase.id).where(
                        TestSuiteCase.suite_id == plan.suite_id, TestSuiteCase.enabled.is_(True)
                    )
                ).scalars().all()
            )
    else:
        payload.resolved_case_count = len(plan.case_ids or [])
    return payload


@router.get("/regression-plans", response_model=list[PlanOut])
def list_plans(project_id: str | None = None, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[PlanOut]:
    query = select(RegressionPlan).order_by(RegressionPlan.created_at.desc())
    if project_id:
        query = query.where(RegressionPlan.project_id == project_id)
    rows = db.execute(query).scalars().all()
    return [_enrich(db, item) for item in rows]


@router.get("/regression-plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> PlanOut:
    plan = db.get(RegressionPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="回归计划不存在")
    return _enrich(db, plan)


@router.post("/regression-plans", response_model=PlanOut, status_code=201)
def create_plan(payload: PlanIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> PlanOut:
    _validate(db, payload)
    plan = RegressionPlan(
        project_id=payload.project_id,
        name=payload.name.strip(),
        description=payload.description,
        environment_id=payload.environment_id,
        suite_id=payload.suite_id,
        case_ids=payload.case_ids or [],
        browser=payload.browser or "chromium",
        trigger_type=payload.trigger_type,
        schedule=payload.schedule,
        retry_count=payload.retry_count,
        fail_retry=payload.fail_retry,
        notification_enabled=payload.notification_enabled,
        enabled=payload.enabled,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _enrich(db, plan)


@router.put("/regression-plans/{plan_id}", response_model=PlanOut)
def update_plan(plan_id: str, payload: PlanIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> PlanOut:
    plan = db.get(RegressionPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="回归计划不存在")
    _validate(db, payload)
    plan.name = payload.name.strip()
    plan.description = payload.description
    plan.environment_id = payload.environment_id
    plan.suite_id = payload.suite_id
    plan.case_ids = payload.case_ids or []
    plan.browser = payload.browser or "chromium"
    plan.trigger_type = payload.trigger_type
    plan.schedule = payload.schedule
    plan.retry_count = payload.retry_count
    plan.fail_retry = payload.fail_retry
    plan.notification_enabled = payload.notification_enabled
    plan.enabled = payload.enabled
    db.commit()
    db.refresh(plan)
    return _enrich(db, plan)


@router.delete("/regression-plans/{plan_id}", status_code=204)
def delete_plan(plan_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> None:
    plan = db.get(RegressionPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="回归计划不存在")
    db.delete(plan)
    db.commit()
