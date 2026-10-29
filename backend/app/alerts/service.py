"""告警引擎：什么时候应该通知、以及 Alert 的状态机与重新验证。

三条不可动摇的规则：

**1. 聚合单位是 FailureGroup，不是失败用例、也不是 Run。**
方案 §12：10 个用例因为同一个根因失败，只应该有一条主要告警；
同一个问题连续出现 10 个 Run，也还是那一条，只是计数在涨。
数据库上用一个**部分唯一索引**兜住（活动告警唯一），并发也不会建出两条。

**2. 引擎永远不会把告警自动置为 RESOLVED。**
「这一轮没再出现」不等于「这个问题修好了」—— 可能是没跑到、可能是环境没起来。
所以 Reverify 之后如果问题消失，告警停在 REVERIFY 等人工确认；
如果问题还在，自动退回 OPEN。只有人能说「解决了」。

**3. 这里的任何失败都不影响测试结果。**
AI 挂了、企业微信挂了，`ExecutionRun` / `CaseResult` / `FailureGroup.category`
一个字段都不会变 —— 它们各自的 catch 只写自己的表。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy import distinct, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    ALERT_ACTIVE_STATUSES,
    ALERT_TRANSITIONS,
    Alert,
    Environment,
    ExecutionRun,
    FailureGroup,
    FailureOccurrence,
    NotificationLog,
    RegressionPlan,
    utcnow,
)
from ..utils.redact import redact_text
from .rules import decide_severity, escalate

logger = logging.getLogger("testflow.alerts")

SUMMARY_LIMIT = 2000


# ------------------------------------------------------------------ 序列化


def serialize_alert(row: Alert) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "failure_group_id": row.failure_group_id,
        "run_id": row.run_id or "",
        "latest_run_id": row.latest_run_id or "",
        "reverify_run_id": row.reverify_run_id or "",
        "severity": row.severity,
        "status": row.status,
        "title": row.title or "",
        "summary": row.summary or "",
        "first_seen_at": row.first_seen_at.isoformat(timespec="seconds") if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat(timespec="seconds") if row.last_seen_at else None,
        "occurrence_count": int(row.occurrence_count or 0),
        "failed_case_count": int(row.failed_case_count or 0),
        "failed_run_count": int(row.failed_run_count or 0),
        "acknowledged_at": row.acknowledged_at.isoformat(timespec="seconds") if row.acknowledged_at else None,
        "resolved_at": row.resolved_at.isoformat(timespec="seconds") if row.resolved_at else None,
        "resolved_by": row.resolved_by or "",
        "allowed_transitions": list(ALERT_TRANSITIONS.get(row.status, ())),
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
    }


# -------------------------------------------------------------------- 核心


def build_alert_summary(group: FailureGroup) -> str:
    parts = [
        f"分类：{group.category}",
        f"命中规则：{group.rule_id or '-'}",
        f"影响用例：{int(group.failed_case_count or 0)}",
        f"影响执行：{int(group.failed_run_count or 0)}",
        f"累计出现：{int(group.occurrence_count or 0)}",
    ]
    if group.rule_reason:
        parts.append(f"规则理由：{group.rule_reason}")
    return redact_text("\n".join(parts))[:SUMMARY_LIMIT]


def upsert_alert(db: Session, group: FailureGroup, *, run_id: str = "") -> tuple[Alert, bool]:
    """为某个失败分组建立或更新告警。返回 (alert, 是否新建)。

    已存在活动告警时**只更新**：计数累加、最新 Run 指向、级别只升不降。
    绝不新开一条 —— 否则一个长期未修的问题会每天刷一条新告警，
    人很快就不看告警了，这正是方案 §12 要避免的。
    """
    decision = decide_severity(group)
    existing = db.execute(
        select(Alert).where(
            Alert.failure_group_id == group.id, Alert.status.in_(list(ALERT_ACTIVE_STATUSES))
        )
    ).scalars().first()

    if existing is not None:
        existing.last_seen_at = utcnow()
        existing.latest_run_id = run_id or existing.latest_run_id
        existing.occurrence_count = int(group.occurrence_count or 0)
        existing.failed_case_count = int(group.failed_case_count or 0)
        existing.failed_run_count = int(group.failed_run_count or 0)
        existing.severity = escalate(existing.severity, decision.severity)
        existing.summary = build_alert_summary(group)
        db.commit()
        db.refresh(existing)
        return existing, False

    row = Alert(
        project_id=group.project_id,
        failure_group_id=group.id,
        run_id=run_id or None,
        latest_run_id=run_id or "",
        severity=decision.severity,
        status="OPEN",
        title=(group.title or group.id)[:255],
        summary=build_alert_summary(group),
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
        occurrence_count=int(group.occurrence_count or 0),
        failed_case_count=int(group.failed_case_count or 0),
        failed_run_count=int(group.failed_run_count or 0),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # 并发下另一个请求刚建好同一个分组的活动告警：回滚后取那一条，
        # 不重建（数据库唯一约束是最终保障，不是唯一保障）
        db.rollback()
        existing = db.execute(
            select(Alert).where(
                Alert.failure_group_id == group.id, Alert.status.in_(list(ALERT_ACTIVE_STATUSES))
            )
        ).scalars().first()
        if existing is None:  # pragma: no cover
            raise
        return existing, False
    db.refresh(row)
    logger.info("新建告警 %s group=%s severity=%s", row.id, group.id, row.severity)
    return row, True


class AlertTransitionError(ValueError):
    """非法状态跳转。"""


def transition(
    db: Session,
    alert: Alert,
    target: str,
    *,
    actor: str = "",
    comment: str = "",
) -> Alert:
    """状态机转换。白名单式，非法跳转直接拒绝。

    `RESOLVED` 与 `IGNORED` 都带人工语义，所以在切换时记下人和时间；
    自动流程**不允许**调用它们（`process_run` 里没有任何一处置 RESOLVED）。
    """
    current = alert.status
    allowed = ALERT_TRANSITIONS.get(current, ())
    if target not in allowed:
        raise AlertTransitionError(f"不允许从 {current} 直接转到 {target}（允许：{'/'.join(allowed) or '无'}）")

    alert.status = target
    if target == "ACKNOWLEDGED":
        alert.acknowledged_at = utcnow()
    elif target == "RESOLVED":
        alert.resolved_at = utcnow()
        alert.resolved_by = str(actor or "")[:64]
        alert.summary = (alert.summary + f"\n\n[解决] {comment}")[:SUMMARY_LIMIT] if comment else alert.summary
    elif target == "IGNORED":
        alert.summary = (alert.summary + f"\n\n[忽略] {comment}")[:SUMMARY_LIMIT] if comment else alert.summary
    db.commit()
    db.refresh(alert)
    return alert


def reverify(db: Session, alert: Alert, *, actor: str = "") -> dict[str, Any]:
    """重新验证：复用 M2 的失败用例重跑，创建一个只跑失败用例的新 Run。

    **不重写重跑逻辑** —— 走的还是 `create_run(source_run_id=...)` 那条路，
    与网页上点「重跑失败用例」是同一套。
    """
    from ..execution.service import create_run, failed_case_ids  # noqa: PLC0415 - 打破循环导入
    from ..workers import dispatch_run  # noqa: PLC0415

    if alert.status not in {"FIXING", "REVERIFY"}:
        raise AlertTransitionError(f"只有 FIXING / REVERIFY 状态的告警可以重新验证，当前是 {alert.status}")

    source_run_id = alert.reverify_run_id or alert.latest_run_id or alert.run_id or ""
    source_run = db.get(ExecutionRun, source_run_id) if source_run_id else None
    if source_run is None:
        raise AlertTransitionError("找不到可用于重新验证的执行记录")

    case_ids = failed_case_ids(db, source_run.id)
    if not case_ids:
        raise AlertTransitionError("原执行里没有失败的用例，无法重新验证（可能问题已经消失，请人工确认后标记已解决）")

    run = create_run(
        db,
        project_id=source_run.project_id,
        plan_id=source_run.plan_id,
        environment_id=source_run.environment_id,
        trigger_source="REVERIFY",
        commit_sha=source_run.commit_sha,
        branch=source_run.branch,
        pipeline_id=source_run.pipeline_id,
        pipeline_url=source_run.pipeline_url,
        case_ids=case_ids,
        source_run_id=source_run.id,
        rerun_mode="verify",
    )
    dispatch_run(run.id, blocking=False)

    alert.reverify_run_id = run.id
    alert.status = "REVERIFY"
    db.commit()
    db.refresh(alert)
    logger.info("告警 %s 进入重新验证：新 Run %s（原 Run %s）", alert.id, run.id, source_run.id)
    return {"alert": alert, "run_id": run.id, "source_run_id": source_run.id, "case_count": len(case_ids), "actor": actor}


# ------------------------------------------------------------- Run 级编排


def groups_in_run(db: Session, run_id: str) -> list[FailureGroup]:
    ids = db.execute(
        select(distinct(FailureOccurrence.failure_group_id)).where(FailureOccurrence.run_id == run_id)
    ).scalars().all()
    if not ids:
        return []
    return list(db.execute(select(FailureGroup).where(FailureGroup.id.in_(list(ids)))).scalars().all())


def evaluate_run(db: Session, run_id: str) -> dict[str, Any]:
    """为一次 Run 涉及的所有失败分组建立/更新告警，并处理 Reverify 回环。

    这一步是**纯确定性**的：不调 AI、不发通知。
    把它单独拆出来，是为了让「告警聚合是否正确」可以被独立测试与复算。
    """
    run = db.get(ExecutionRun, run_id)
    if run is None:
        return {"run_id": run_id, "evaluated": False, "reason": "run_not_found"}

    groups = groups_in_run(db, run_id)
    created = 0
    updated = 0
    for group in groups:
        _alert, is_new = upsert_alert(db, group, run_id=run_id)
        created += 1 if is_new else 0
        updated += 0 if is_new else 1

    # Reverify 回环：这次 Run 是某条告警的重新验证执行
    reopened = 0
    verified = 0
    pending = db.execute(
        select(Alert).where(Alert.status == "REVERIFY", Alert.reverify_run_id == run_id)
    ).scalars().all()
    appeared = {group.id for group in groups}
    for alert in pending:
        if alert.failure_group_id in appeared:
            # 问题又出现了 → 退回 OPEN。绝不能因为「跑完了」就当修好了
            alert.status = "OPEN"
            alert.last_seen_at = utcnow()
            db.commit()
            reopened += 1
        else:
            # 问题没再出现 → 停在 REVERIFY 等人工确认（引擎不自动 RESOLVED）
            verified += 1

    return {
        "run_id": run_id,
        "evaluated": True,
        "groups": len(groups),
        "alerts_created": created,
        "alerts_updated": updated,
        "reverify_reopened": reopened,
        "reverify_pending_human": verified,
    }


def _should_notify(db: Session, run: ExecutionRun) -> tuple[bool, str]:
    """该不该发通知。两层：全局配置 + 计划开关。"""
    if not settings.wecom_enabled:
        return False, "企业微信未配置或通知总开关关闭"
    plan = db.get(RegressionPlan, run.plan_id) if run.plan_id else None
    if plan is not None and not plan.notification_enabled:
        # 默认静默：现有字段 notification_enabled 的语义就是「这个计划失败时要不要发通知」
        return False, f"回归计划「{plan.name}」未开启企业微信通知"
    return True, ""


def process_run(db: Session, run_id: str, *, analyze: bool = True, notify: bool = True) -> dict[str, Any]:
    """M5 的完整后置流程：告警 → AI 分析 → 通知。

    **每一步都能独立失败且互不牵连。** AI 挂了照发通知，通知挂了结论照样入库。
    这个方法本身也不抛异常 —— 它跑在 Run 结束之后，抛出去会影响调用方。
    """
    result: dict[str, Any] = {"run_id": run_id, "analyze": analyze, "notify": notify}
    try:
        result["alerts"] = evaluate_run(db, run_id)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.exception("告警评估失败 run=%s", run_id)
        result["alerts"] = {"evaluated": False, "error": f"{type(exc).__name__}: {exc}"}

    run = db.get(ExecutionRun, run_id)
    if run is None:
        result["reason"] = "run_not_found"
        return result

    # ---- AI 分析：只为需要它的分组做（默认 UNKNOWN）
    analyzed: list[dict[str, Any]] = []
    if analyze and settings.ai_auto_analyze:
        from ..ai import service as ai_service  # noqa: PLC0415 - 打破循环导入

        for group in groups_in_run(db, run_id):
            try:
                outcome = ai_service.analyze_group(db, group.id)
            except Exception as exc:  # noqa: BLE001 - 单组失败不影响其它组
                db.rollback()
                logger.exception("AI 分析异常 group=%s", group.id)
                outcome = {"group_id": group.id, "status": "FAILED", "error_message": f"{type(exc).__name__}"}
            analyzed.append(outcome)
    result["analyzed"] = analyzed

    # ---- 通知
    notified: list[dict[str, Any]] = []
    if notify:
        allowed, reason = _should_notify(db, run)
        result["notify_skipped_reason"] = "" if allowed else reason
        if allowed:
            from ..notification import service as notification_service  # noqa: PLC0415

            alerts = db.execute(
                select(Alert).where(Alert.latest_run_id == run_id, Alert.status.in_(list(ALERT_ACTIVE_STATUSES)))
            ).scalars().all()
            for alert in alerts:
                try:
                    notified.append(notification_service.send_alert(db, alert))
                except Exception as exc:  # noqa: BLE001 - 发送失败绝不影响 Run
                    db.rollback()
                    logger.exception("通知发送异常 alert=%s", alert.id)
                    notified.append({"alert_id": alert.id, "status": "FAILED", "error": f"{type(exc).__name__}"})
    result["notified"] = notified
    return result


def on_run_completed_async(run_id: str) -> None:
    """Run 结束后在**独立后台线程**里跑 M5 流程。

    为什么必须异步：AI 分析是网络调用，可能几十秒；企业微信还可能重试。
    如果串在 `execute_run_sync` 的返回路径上，CLI 的 `--wait` 就要多等几十秒，
    而且「测试平台慢」会被误以为是「测试跑得慢」。
    这里用后台线程而不是 Celery —— 方案 §19 明确说不为 M5 引入新的任务基础设施。
    """
    if not (settings.ai_auto_analyze or settings.wecom_enabled):
        return

    def _worker() -> None:
        from ..db import SessionLocal  # noqa: PLC0415

        db = SessionLocal()
        try:
            process_run(db, run_id)
        except Exception:  # noqa: BLE001 - 后台线程里的异常只能自己吃掉
            logger.exception("M5 后置流程异常 run=%s", run_id)
        finally:
            db.close()

    threading.Thread(target=_worker, name=f"m5-{run_id}", daemon=True).start()


def list_run_notifications(db: Session, run_id: str) -> list[NotificationLog]:
    """按 Run 反查通知记录（Failuregroup → Alert → Log 三段太绕，直接查）。"""
    alert_ids = db.execute(select(Alert.id).where(Alert.latest_run_id == run_id)).scalars().all()
    if not alert_ids:
        return []
    return list(
        db.execute(
            select(NotificationLog).where(NotificationLog.alert_id.in_(list(alert_ids))).order_by(NotificationLog.created_at.desc())
        ).scalars().all()
    )


__all__ = [
    "AlertTransitionError",
    "build_alert_summary",
    "evaluate_run",
    "groups_in_run",
    "list_run_notifications",
    "on_run_completed_async",
    "process_run",
    "reverify",
    "serialize_alert",
    "transition",
    "upsert_alert",
]
