"""M5 告警与通知接口。

```
GET    /api/alerts                      告警列表（分页 + 状态/严重级别/项目筛选）
GET    /api/alerts/{id}                 告警详情
POST   /api/failures/{id}/alerts        为某个失败分组建立/刷新告警（幂等）
POST   /api/alerts/{id}/ack             确认
POST   /api/alerts/{id}/fixing          开始处理
POST   /api/alerts/{id}/reverify        重新验证（复用 M2 失败用例重跑）
POST   /api/alerts/{id}/resolve         标记已解决（人工）
POST   /api/alerts/{id}/ignore          忽略（人工）
POST   /api/alerts/{id}/notify          手工补发通知（CI 之外的补救入口）
GET    /api/alerts/{id}/notifications   该告警的通知投递记录
```

状态转换是**白名单式**的：每个动作一个接口，非法跳转返回 400 并说明允许的下一步。
特别地 `RESOLVED` 只能从 `REVERIFY` 来 —— 不允许「OPEN 直接解决」，
那等于没验证就宣称修好了；而引擎自己也从不自动置 RESOLVED。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..alerts import service as alert_service
from ..alerts.rules import decide_severity
from ..auth.deps import get_current_user, require_writer
from ..db import get_db
from ..models import (
    ALERT_ACTIVE_STATUSES,
    ALERT_SEVERITIES,
    ALERT_STATUSES,
    Alert,
    FailureGroup,
    User,
)
from ..notification import service as notification_service
from ..schemas import AlertTransitionRequest

router = APIRouter(prefix="/api", tags=["m5-alerts"])

MAX_PAGE_SIZE = 200


def _require_alert(db: Session, alert_id: str) -> Alert:
    row = db.get(Alert, alert_id)
    if row is None:
        raise HTTPException(status_code=404, detail="告警不存在")
    return row


def _transition(db: Session, alert: Alert, target: str, request: AlertTransitionRequest | None, user: User) -> dict[str, Any]:
    try:
        row = alert_service.transition(
            db, alert, target, actor=user.username, comment=(request.comment if request else "")
        )
    except alert_service.AlertTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return alert_service.serialize_alert(row)


@router.get("/alerts")
def list_alerts(
    project_id: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    active_only: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """告警列表。默认按最近出现时间倒序 —— 还在发生的问题排最前。"""
    query = select(Alert)
    if project_id:
        query = query.where(Alert.project_id == project_id)
    if status:
        target = status.strip().upper()
        if target not in ALERT_STATUSES:
            raise HTTPException(status_code=400, detail=f"status 必须是 {ALERT_STATUSES} 之一")
        query = query.where(Alert.status == target)
    elif active_only:
        query = query.where(Alert.status.in_(list(ALERT_ACTIVE_STATUSES)))
    if severity:
        target = severity.strip().upper()
        if target not in ALERT_SEVERITIES:
            raise HTTPException(status_code=400, detail=f"severity 必须是 {ALERT_SEVERITIES} 之一")
        query = query.where(Alert.severity == target)

    total = int(
        db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar() or 0
    )
    rows = db.execute(
        query.order_by(Alert.last_seen_at.desc(), Alert.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    ).scalars().all()

    # 状态/级别分布刻意忽略对应筛选，否则一切换其它 Tab 数字就全变 0
    base = select(Alert)
    if project_id:
        base = base.where(Alert.project_id == project_id)
    sub = base.with_only_columns(Alert.status, Alert.severity).subquery()
    status_counts = {
        str(row[0]): int(row[1])
        for row in db.execute(select(sub.c.status, func.count()).group_by(sub.c.status)).all()
    }
    severity_counts = {
        str(row[0]): int(row[1])
        for row in db.execute(select(sub.c.severity, func.count()).group_by(sub.c.severity)).all()
    }

    return {
        "items": [alert_service.serialize_alert(row) for row in rows],
        "total": total,
        "page": max(1, page),
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size,
        "status_counts": status_counts,
        "severity_counts": severity_counts,
    }


@router.get("/alerts/{alert_id}")
def get_alert(
    alert_id: str,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    alert = _require_alert(db, alert_id)
    payload = alert_service.serialize_alert(alert)
    group = db.get(FailureGroup, alert.failure_group_id)
    payload["failure_group"] = (
        {
            "id": group.id,
            "title": group.title,
            "category": group.category,
            "rule_id": group.rule_id,
            "rule_reason": group.rule_reason,
            "occurrence_count": int(group.occurrence_count or 0),
        }
        if group is not None
        else None
    )
    severity = decide_severity(group) if group is not None else None
    payload["severity_rule"] = (
        {"severity": severity.severity, "rule_id": severity.rule_id, "reason": severity.reason}
        if severity
        else None
    )
    return payload


@router.post("/failures/{group_id}/alerts")
def create_or_refresh_alert(
    group_id: str,
    _: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """为某个失败分组建立或刷新告警。**幂等**：已有活动告警就更新，不新建。"""
    group = db.get(FailureGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="失败分组不存在")
    alert, created = alert_service.upsert_alert(db, group, run_id=group.latest_run_id or "")
    payload = alert_service.serialize_alert(alert)
    payload["created"] = created
    return payload


@router.post("/alerts/{alert_id}/ack")
def acknowledge(
    alert_id: str,
    payload: AlertTransitionRequest | None = None,
    user: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _transition(db, _require_alert(db, alert_id), "ACKNOWLEDGED", payload, user)


@router.post("/alerts/{alert_id}/fixing")
def start_fixing(
    alert_id: str,
    payload: AlertTransitionRequest | None = None,
    user: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _transition(db, _require_alert(db, alert_id), "FIXING", payload, user)


@router.post("/alerts/{alert_id}/reverify")
def reverify(
    alert_id: str,
    _: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """重新验证：新建一个只跑原失败用例的 Run。

    **复用 M2 的失败用例重跑**（`create_run(source_run_id=...)`），没有另写一套。
    新 Run 与原告警建立关系（`alert.reverify_run_id`），原 Run 一个字节不改。
    """
    alert = _require_alert(db, alert_id)
    try:
        outcome = alert_service.reverify(db, alert)
    except alert_service.AlertTransitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "alert": alert_service.serialize_alert(outcome["alert"]),
        "run_id": outcome["run_id"],
        "source_run_id": outcome["source_run_id"],
        "case_count": outcome["case_count"],
    }


@router.post("/alerts/{alert_id}/resolve")
def resolve(
    alert_id: str,
    payload: AlertTransitionRequest | None = None,
    user: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """人工标记已解决。只能从 REVERIFY 来 —— 必须经过一次重新验证。"""
    return _transition(db, _require_alert(db, alert_id), "RESOLVED", payload, user)


@router.post("/alerts/{alert_id}/ignore")
def ignore(
    alert_id: str,
    payload: AlertTransitionRequest | None = None,
    user: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _transition(db, _require_alert(db, alert_id), "IGNORED", payload, user)


@router.post("/alerts/{alert_id}/notify")
def notify_now(
    alert_id: str,
    force: bool = Query(default=False, description="true=同一内容也再发一次"),
    _: User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """手工补发通知（同步执行，便于立刻看到结果）。

    存在两个理由：① 自动发送时渠道没配好，配好之后要能补发；
    ② 给真实企业微信的 smoke test 一个入口。
    """
    alert = _require_alert(db, alert_id)
    return notification_service.send_alert(db, alert, force=force)


@router.get("/alerts/{alert_id}/notifications")
def list_alert_notifications(
    alert_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    _require_alert(db, alert_id)
    rows = notification_service.list_notifications(db, alert_id, limit=limit)
    return {"alert_id": alert_id, "items": [notification_service.serialize_notification(row) for row in rows]}


__all__ = ["router"]
