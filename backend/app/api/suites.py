from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..auth.deps import get_current_user, require_writer
from ..db import get_db
from ..models import CaseFlowBinding, Project, TestCase, TestSuite, TestSuiteCase, User
from ..schemas import SuiteCaseIn, SuiteMemberOut, SuiteOrderIn, TestSuiteIn, TestSuiteOut

router = APIRouter(prefix="/api", tags=["suites"])


def _case_index(db: Session, case_ids: list[str]) -> dict[str, dict[str, str]]:
    """批量取用例信息 + 绑定流程，避免逐个查询。"""
    if not case_ids:
        return {}
    cases = db.execute(select(TestCase).where(TestCase.id.in_(case_ids))).scalars().all()
    flow_ids = {
        item.case_id: item.flow_id
        for item in db.execute(
            select(CaseFlowBinding).where(
                CaseFlowBinding.case_id.in_(case_ids), CaseFlowBinding.enabled.is_(True)
            )
        ).scalars().all()
    }
    flows: dict[str, str] = {}
    if flow_ids:
        from ..models import AutomationFlow  # noqa: PLC0415

        flows = {
            item.id: item.name
            for item in db.execute(select(AutomationFlow).where(AutomationFlow.id.in_(list(flow_ids.values())))).scalars().all()
        }
    index: dict[str, dict[str, str]] = {}
    for case in cases:
        flow_id = flow_ids.get(case.id, "")
        index[case.id] = {
            "case_code": case.case_code or "",
            "case_name": case.name or "",
            "module": case.module or "",
            "priority": case.priority or "P2",
            "case_status": case.status or "",
            "flow_id": flow_id,
            "flow_name": flows.get(flow_id, ""),
        }
    return index


def _serialize(suite: TestSuite, index: dict[str, dict[str, str]]) -> TestSuiteOut:
    payload = TestSuiteOut.model_validate(suite)
    members: list[SuiteMemberOut] = []
    for member in sorted(suite.members, key=lambda item: (item.sort_order, item.id)):
        info = index.get(member.case_id, {})
        members.append(
            SuiteMemberOut(
                id=member.id,
                case_id=member.case_id,
                sort_order=member.sort_order,
                enabled=member.enabled,
                case_code=info.get("case_code", ""),
                case_name=info.get("case_name", "(用例已被删除)"),
                module=info.get("module", ""),
                priority=info.get("priority", "P2"),
                case_status=info.get("case_status", ""),
                flow_id=info.get("flow_id", ""),
                flow_name=info.get("flow_name", ""),
            )
        )
    payload.members = members
    payload.case_count = len(members)
    payload.enabled_case_count = sum(1 for item in members if item.enabled)
    return payload


@router.get("/test-suites", response_model=list[TestSuiteOut])
def list_suites(
    project_id: str | None = None,
    q: str = "",
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TestSuiteOut]:
    query = select(TestSuite).options(selectinload(TestSuite.members)).order_by(TestSuite.created_at.desc())
    if project_id:
        query = query.where(TestSuite.project_id == project_id)
    if q:
        query = query.where(TestSuite.name.ilike(f"%{q.strip()}%"))
    suites = db.execute(query).scalars().all()
    all_case_ids = [member.case_id for suite in suites for member in suite.members]
    index = _case_index(db, all_case_ids)
    return [_serialize(suite, index) for suite in suites]


@router.get("/test-suites/{suite_id}", response_model=TestSuiteOut)
def get_suite(suite_id: str, _: User = Depends(get_current_user), db: Session = Depends(get_db)) -> TestSuiteOut:
    suite = db.execute(
        select(TestSuite).options(selectinload(TestSuite.members)).where(TestSuite.id == suite_id)
    ).scalars().first()
    if suite is None:
        raise HTTPException(status_code=404, detail="测试套件不存在")
    return _serialize(suite, _case_index(db, [member.case_id for member in suite.members]))


@router.post("/test-suites", response_model=TestSuiteOut, status_code=201)
def create_suite(payload: TestSuiteIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> TestSuiteOut:
    if db.get(Project, payload.project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    suite = TestSuite(
        project_id=payload.project_id,
        name=payload.name.strip(),
        description=payload.description,
        enabled=payload.enabled,
    )
    seen: set[str] = set()
    order = 1
    for case_id in payload.case_ids or []:
        if case_id in seen:
            continue
        if db.get(TestCase, case_id) is None:
            raise HTTPException(status_code=404, detail=f"测试用例不存在：{case_id}")
        seen.add(case_id)
        suite.members.append(TestSuiteCase(case_id=case_id, sort_order=order, enabled=True))
        order += 1
    db.add(suite)
    db.commit()
    db.refresh(suite)
    return _serialize(suite, _case_index(db, [member.case_id for member in suite.members]))


@router.put("/test-suites/{suite_id}", response_model=TestSuiteOut)
def update_suite(suite_id: str, payload: TestSuiteIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> TestSuiteOut:
    suite = db.execute(
        select(TestSuite).options(selectinload(TestSuite.members)).where(TestSuite.id == suite_id)
    ).scalars().first()
    if suite is None:
        raise HTTPException(status_code=404, detail="测试套件不存在")
    suite.name = payload.name.strip()
    suite.description = payload.description
    suite.enabled = payload.enabled
    # case_ids 传了才同步成员；传空列表表示清空
    if payload.case_ids is not None:
        existing = {member.case_id: member for member in suite.members}
        suite.members.clear()
        db.flush()
        order = 1
        for case_id in payload.case_ids:
            if db.get(TestCase, case_id) is None:
                raise HTTPException(status_code=404, detail=f"测试用例不存在：{case_id}")
            previous = existing.get(case_id)
            suite.members.append(
                TestSuiteCase(case_id=case_id, sort_order=order, enabled=previous.enabled if previous else True)
            )
            order += 1
    db.commit()
    db.refresh(suite)
    return _serialize(suite, _case_index(db, [member.case_id for member in suite.members]))


@router.delete("/test-suites/{suite_id}", status_code=204)
def delete_suite(suite_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> None:
    suite = db.get(TestSuite, suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="测试套件不存在")
    db.delete(suite)
    db.commit()


@router.post("/test-suites/{suite_id}/cases", response_model=TestSuiteOut, status_code=201)
def add_suite_case(suite_id: str, payload: SuiteCaseIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> TestSuiteOut:
    suite = db.execute(
        select(TestSuite).options(selectinload(TestSuite.members)).where(TestSuite.id == suite_id)
    ).scalars().first()
    if suite is None:
        raise HTTPException(status_code=404, detail="测试套件不存在")
    if db.get(TestCase, payload.case_id) is None:
        raise HTTPException(status_code=404, detail="测试用例不存在")
    existing = next((item for item in suite.members if item.case_id == payload.case_id), None)
    if existing is not None:
        existing.enabled = payload.enabled
        if payload.sort_order:
            existing.sort_order = payload.sort_order
    else:
        next_order = max((item.sort_order for item in suite.members), default=0) + 1
        suite.members.append(
            TestSuiteCase(case_id=payload.case_id, sort_order=payload.sort_order or next_order, enabled=payload.enabled)
        )
    db.commit()
    db.refresh(suite)
    return _serialize(suite, _case_index(db, [member.case_id for member in suite.members]))


@router.delete("/test-suites/{suite_id}/cases/{case_id}", response_model=TestSuiteOut)
def remove_suite_case(suite_id: str, case_id: str, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> TestSuiteOut:
    suite = db.execute(
        select(TestSuite).options(selectinload(TestSuite.members)).where(TestSuite.id == suite_id)
    ).scalars().first()
    if suite is None:
        raise HTTPException(status_code=404, detail="测试套件不存在")
    target = next((item for item in suite.members if item.case_id == case_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="该用例不在套件中")
    suite.members.remove(target)
    db.commit()
    db.refresh(suite)
    return _serialize(suite, _case_index(db, [member.case_id for member in suite.members]))


@router.put("/test-suites/{suite_id}/order", response_model=TestSuiteOut)
def reorder_suite(suite_id: str, payload: SuiteOrderIn, _: User = Depends(require_writer), db: Session = Depends(get_db)) -> TestSuiteOut:
    """一次性提交顺序与启用状态，避免前端拖拽时发一串请求。"""
    suite = db.execute(
        select(TestSuite).options(selectinload(TestSuite.members)).where(TestSuite.id == suite_id)
    ).scalars().first()
    if suite is None:
        raise HTTPException(status_code=404, detail="测试套件不存在")
    index = {item.case_id: item for item in suite.members}
    for item in payload.items:
        target = index.get(item.case_id)
        if target is None:
            continue
        target.sort_order = item.sort_order
        target.enabled = item.enabled
    db.commit()
    db.refresh(suite)
    return _serialize(suite, _case_index(db, [member.case_id for member in suite.members]))
