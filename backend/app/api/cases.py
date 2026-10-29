from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..auth.deps import get_current_user, require_writer
from ..db import get_db
from ..models import AutomationFlow, CaseFlowBinding, TestCase, TestStep, User
from ..schemas import BindingIn, BindingOut, TestCaseIn, TestCaseOut

router = APIRouter(prefix="/api", tags=["cases"])

DEFAULT_STEPS = [
    {"sort_order": 1, "action": "open", "value": "${base_url}/login", "expected": "登录页打开", "screenshot": True},
    {"sort_order": 2, "action": "fill", "element_id": "login-username", "value": "${username}", "expected": "用户名输入成功"},
    {"sort_order": 3, "action": "fill", "element_id": "login-password", "value": "${password}", "expected": "密码输入成功"},
    {"sort_order": 4, "action": "click", "element_id": "login-submit", "expected": "点击登录按钮"},
    {"sort_order": 5, "action": "assert_text", "element_id": "home-greeting", "expected": "登录成功", "screenshot": True},
]


def _serialize(case: TestCase, flow: AutomationFlow | None = None) -> TestCaseOut:
    payload = TestCaseOut.model_validate(case)
    payload.steps = [item for item in payload.steps]
    if flow is not None:
        payload.flow_id = flow.id
        payload.flow_name = flow.name
    return payload


def _bound_flows(db: Session, case_ids: list[str]) -> dict[str, AutomationFlow]:
    if not case_ids:
        return {}
    bindings = db.execute(
        select(CaseFlowBinding).where(CaseFlowBinding.case_id.in_(case_ids), CaseFlowBinding.enabled.is_(True))
    ).scalars().all()
    flow_ids = {item.flow_id for item in bindings}
    if not flow_ids:
        return {}
    flows = db.execute(select(AutomationFlow).where(AutomationFlow.id.in_(flow_ids))).scalars().all()
    flow_map = {item.id: item for item in flows}
    return {item.case_id: flow_map[item.flow_id] for item in bindings if item.flow_id in flow_map}


@router.get("/test-cases", response_model=list[TestCaseOut])
def list_test_cases(
    project_id: str | None = None,
    q: str = "",
    module: str = "",
    priority: str = "",
    status: str = "",
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TestCaseOut]:
    query = select(TestCase).options(selectinload(TestCase.steps)).order_by(TestCase.created_at.desc())
    if project_id:
        query = query.where(TestCase.project_id == project_id)
    if module:
        query = query.where(TestCase.module == module)
    if priority:
        query = query.where(TestCase.priority == priority)
    if status:
        query = query.where(TestCase.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(TestCase.name.ilike(like) | TestCase.case_code.ilike(like) | TestCase.module.ilike(like))
    rows = db.execute(query).scalars().all()
    flow_map = _bound_flows(db, [item.id for item in rows])
    return [_serialize(item, flow_map.get(item.id)) for item in rows]


@router.get("/test-cases/{case_id}", response_model=TestCaseOut)
def get_test_case(case_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> TestCaseOut:
    case = db.execute(
        select(TestCase).options(selectinload(TestCase.steps)).where(TestCase.id == case_id)
    ).scalars().first()
    if case is None:
        raise HTTPException(status_code=404, detail="测试用例不存在")
    flow_map = _bound_flows(db, [case.id])
    return _serialize(case, flow_map.get(case.id))


@router.post("/test-cases", response_model=TestCaseOut, status_code=201)
def create_test_case(payload: TestCaseIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> TestCaseOut:
    case = TestCase(
        project_id=payload.project_id,
        case_code=payload.case_code,
        name=payload.name.strip(),
        module=payload.module,
        priority=payload.priority,
        status=payload.status,
        tags=payload.tags or [],
        description=payload.description,
        preconditions=payload.preconditions,
        expected=payload.expected,
        created_by=payload.created_by,
    )
    steps = payload.steps or []
    for index, step in enumerate(steps, 1):
        case.steps.append(
            TestStep(
                sort_order=step.sort_order or index,
                action=step.action,
                element_id=step.element_id,
                value=step.value,
                expected=step.expected,
                timeout=step.timeout,
                retry=step.retry,
                screenshot=step.screenshot,
            )
        )
    db.add(case)
    db.commit()
    db.refresh(case)
    return _serialize(case)


@router.put("/test-cases/{case_id}", response_model=TestCaseOut)
def update_test_case(case_id: str, payload: TestCaseIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> TestCaseOut:
    case = db.execute(
        select(TestCase).options(selectinload(TestCase.steps)).where(TestCase.id == case_id)
    ).scalars().first()
    if case is None:
        raise HTTPException(status_code=404, detail="测试用例不存在")
    case.name = payload.name.strip()
    case.case_code = payload.case_code
    case.module = payload.module
    case.priority = payload.priority
    case.status = payload.status
    case.tags = payload.tags or []
    case.description = payload.description
    case.preconditions = payload.preconditions
    case.expected = payload.expected
    case.steps.clear()
    db.flush()
    for index, step in enumerate(payload.steps or [], 1):
        case.steps.append(
            TestStep(
                sort_order=step.sort_order or index,
                action=step.action,
                element_id=step.element_id,
                value=step.value,
                expected=step.expected,
                timeout=step.timeout,
                retry=step.retry,
                screenshot=step.screenshot,
            )
        )
    db.commit()
    db.refresh(case)
    flow_map = _bound_flows(db, [case.id])
    return _serialize(case, flow_map.get(case.id))


@router.delete("/test-cases/{case_id}", status_code=204)
def delete_test_case(case_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> None:
    case = db.get(TestCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="测试用例不存在")
    db.delete(case)
    db.commit()


@router.post("/test-cases/{case_id}/duplicate", response_model=TestCaseOut, status_code=201)
def duplicate_test_case(case_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> TestCaseOut:
    case = db.execute(
        select(TestCase).options(selectinload(TestCase.steps)).where(TestCase.id == case_id)
    ).scalars().first()
    if case is None:
        raise HTTPException(status_code=404, detail="测试用例不存在")
    clone = TestCase(
        project_id=case.project_id,
        case_code=f"{case.case_code}-COPY" if case.case_code else "",
        name=f"{case.name}（副本）",
        module=case.module,
        priority=case.priority,
        status=case.status,
        tags=list(case.tags or []),
        description=case.description,
        preconditions=case.preconditions,
        expected=case.expected,
        created_by=case.created_by,
    )
    for step in case.steps:
        clone.steps.append(
            TestStep(
                sort_order=step.sort_order,
                action=step.action,
                element_id=step.element_id,
                value=step.value,
                expected=step.expected,
                timeout=step.timeout,
                retry=step.retry,
                screenshot=step.screenshot,
            )
        )
    db.add(clone)
    db.commit()
    db.refresh(clone)
    return _serialize(clone)


@router.get("/case-flow-bindings", response_model=list[BindingOut])
def list_bindings(
    case_id: str | None = None,
    flow_id: str | None = None,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BindingOut]:
    query = select(CaseFlowBinding)
    if case_id:
        query = query.where(CaseFlowBinding.case_id == case_id)
    if flow_id:
        query = query.where(CaseFlowBinding.flow_id == flow_id)
    rows = db.execute(query).scalars().all()
    return [BindingOut.model_validate(item) for item in rows]


@router.post("/case-flow-bindings", response_model=BindingOut, status_code=201)
def upsert_binding(payload: BindingIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> BindingOut:
    if db.get(TestCase, payload.case_id) is None:
        raise HTTPException(status_code=404, detail="测试用例不存在")
    if db.get(AutomationFlow, payload.flow_id) is None:
        raise HTTPException(status_code=404, detail="自动化流程不存在")
    binding = db.execute(
        select(CaseFlowBinding).where(
            CaseFlowBinding.case_id == payload.case_id, CaseFlowBinding.flow_id == payload.flow_id
        )
    ).scalars().first()
    if binding is None:
        binding = CaseFlowBinding(case_id=payload.case_id, flow_id=payload.flow_id, enabled=payload.enabled)
        db.add(binding)
    else:
        binding.enabled = payload.enabled
    db.commit()
    db.refresh(binding)
    return BindingOut.model_validate(binding)


@router.delete("/case-flow-bindings/{binding_id}", status_code=204)
def delete_binding(binding_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> None:
    binding = db.get(CaseFlowBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="绑定不存在")
    db.delete(binding)
    db.commit()
