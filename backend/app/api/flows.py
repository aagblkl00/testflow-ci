from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.deps import get_current_user, require_writer
from ..db import get_db
from ..execution.action_spec import ACTION_LABELS, LOCATOR_PRIORITY, normalize_steps
from ..models import AutomationFlow, Project, User
from ..schemas import FlowIn, FlowOut

router = APIRouter(prefix="/api", tags=["flows"])

DEMO_FLOW_STEPS = [
    {"name": "打开登录页", "action": "open", "value": "${base_url}/login", "timeout": 15000, "screenshot": True},
    {"name": "输入用户名", "action": "fill", "locator_type": "testid", "locator_value": "login-username", "value": "${username}"},
    {"name": "输入密码", "action": "fill", "locator_type": "testid", "locator_value": "login-password", "value": "${password}"},
    {"name": "点击登录", "action": "click", "locator_type": "testid", "locator_value": "login-submit"},
    {"name": "断言登录成功", "action": "assert_visible", "locator_type": "testid", "locator_value": "home-greeting", "timeout": 15000, "screenshot": True},
]


@router.get("/flows/meta")
def flow_meta(_: User = Depends(get_current_user)) -> dict[str, object]:
    """给前端「步骤编辑器」用的动作与定位器字典。"""
    return {
        "actions": [{"value": key, "label": label} for key, label in ACTION_LABELS.items()],
        "locator_priority": list(LOCATOR_PRIORITY),
        "demo_steps": DEMO_FLOW_STEPS,
    }


@router.get("/flows", response_model=list[FlowOut])
def list_flows(project_id: str | None = None, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[FlowOut]:
    query = select(AutomationFlow).order_by(AutomationFlow.created_at.desc())
    if project_id:
        query = query.where(AutomationFlow.project_id == project_id)
    rows = db.execute(query).scalars().all()
    return [FlowOut.model_validate(item) for item in rows]


@router.get("/flows/{flow_id}", response_model=FlowOut)
def get_flow(flow_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> FlowOut:
    flow = db.get(AutomationFlow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="自动化流程不存在")
    return FlowOut.model_validate(flow)


@router.post("/flows", response_model=FlowOut, status_code=201)
def create_flow(payload: FlowIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> FlowOut:
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    steps = normalize_steps(payload.steps)
    flow = AutomationFlow(
        project_id=payload.project_id,
        name=payload.name.strip(),
        engine=payload.engine or "playwright",
        environment_id=payload.environment_id or "",
        variables=payload.variables or {},
        definition={"steps": [item.to_dict() for item in steps]},
        version=1,
    )
    db.add(flow)
    db.commit()
    db.refresh(flow)
    return FlowOut.model_validate(flow)


@router.put("/flows/{flow_id}", response_model=FlowOut)
def update_flow(flow_id: str, payload: FlowIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> FlowOut:
    flow = db.get(AutomationFlow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="自动化流程不存在")
    old_steps = (flow.definition or {}).get("steps") or []
    steps = normalize_steps(payload.steps)
    new_steps = [item.to_dict() for item in steps]
    flow.name = payload.name.strip()
    flow.engine = payload.engine or flow.engine
    flow.environment_id = payload.environment_id or ""
    flow.variables = payload.variables or {}
    flow.definition = {"steps": new_steps}
    if old_steps != new_steps:
        flow.version = int(flow.version or 1) + 1
    db.commit()
    db.refresh(flow)
    return FlowOut.model_validate(flow)


@router.delete("/flows/{flow_id}", status_code=204)
def delete_flow(flow_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> None:
    flow = db.get(AutomationFlow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="自动化流程不存在")
    db.delete(flow)
    db.commit()
