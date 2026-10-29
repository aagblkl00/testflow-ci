"""通知服务：渲染消息、去重、重试、记账。

分工很清楚：

    渲染   —— 一条告警一条消息，按方案 §17 的格式，**以 FailureGroup 为单位**
    去重   —— 同一告警 + 同一渠道 + 同一内容只发一次（内容变了才再发）
    重试   —— 最多 N 次，递增等待，只重试「值得重试」的错误
    记账   —— notification_logs，每次尝试都更新在**同一行**上，重试次数才数得清

**发送失败只写自己这张表。** 不改 Alert 状态、不改 Run、不改任何测试结果 ——
送信失败和问题是否修复之间没有任何因果关系（方案 §18 写得很明确）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Alert, FailureAnalysis, FailureGroup, NotificationLog, RegressionPlan, utcnow
from ..utils.redact import redact_text
from .provider import (
    NotificationMessage,
    NotificationProviderError,
    default_provider_name,
    get_provider,
)

logger = logging.getLogger("testflow.notification")

# 企业微信 markdown 上限 4096 字节，留出余量
CONTENT_CHAR_LIMIT = 1800
# 递增等待的上限，避免后台线程睡太久
MAX_BACKOFF_SECONDS = 4.0

AI_SECTION_LIMIT = 400


def _short(value: str, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def render_alert_message(db: Session, alert: Alert) -> str:
    """渲染企微消息。**一条告警一条消息**，聚合在 FailureGroup 这一层。

    内容里的每一个字段都来自已经脱敏过的数据，另外这里再过一次 redact ——
    「上游已经脱敏了」是假设，「这里也脱一次」是保障。代价是一次正则。
    """
    group = db.get(FailureGroup, alert.failure_group_id)
    plan = None
    run = None
    environment_name = "-"
    if alert.latest_run_id:
        from ..models import Environment, ExecutionRun  # noqa: PLC0415

        run = db.get(ExecutionRun, alert.latest_run_id)
        if run and run.plan_id:
            plan = db.get(RegressionPlan, run.plan_id)
        if run and run.environment_id:
            environment = db.get(Environment, run.environment_id)
            if environment is not None:
                environment_name = environment.name

    lines: list[str] = [
        "# TestFlowCI 回归失败",
        "",
        f"**项目**：{alert.project_id}",
        f"**计划**：{plan.name if plan else '-'}",
        f"**环境**：{environment_name}",
        f"**失败组**：{alert.id}",
        f"**严重级别**：{alert.severity}",
        "",
        f"**失败用例**：{int(alert.failed_case_count or 0)}",
        f"**影响执行**：{int(alert.failed_run_count or 0)}",
        "",
        f"**失败原因**：{_short(group.title if group else '', 120)}",
        f"**分类**：{(group.category if group else 'UNKNOWN')}",
    ]
    if group and group.rule_id:
        lines.append(f"**命中规则**：{group.rule_id}")
    if run is not None and (run.branch or run.commit_sha):
        lines.append(f"**分支/提交**：{run.branch or '-'} / {_short(run.commit_sha, 12) or '-'}")

    # AI 分析（如果有已完成的）
    if group is not None:
        analysis = db.execute(
            select(FailureAnalysis)
            .where(FailureAnalysis.failure_group_id == group.id, FailureAnalysis.status == "COMPLETED")
            .order_by(FailureAnalysis.revision_no.desc())
        ).scalars().first()
        if analysis is not None:
            lines += ["", "**AI 分析**：", _short(analysis.summary, AI_SECTION_LIMIT)]
            if analysis.confidence is not None:
                lines.append(f"**置信度**：{round(float(analysis.confidence) * 100)}%")
            if analysis.recommendation:
                lines.append(f"**建议**：{_short(analysis.recommendation, AI_SECTION_LIMIT)}")
            if analysis.related_commit:
                lines.append(f"**相关提交**：{analysis.related_commit}")

    # 证据
    if group is not None:
        evidence_lines = []
        if group.fingerprint_ref is not None:
            fp = group.fingerprint_ref
            if fp.request_method or fp.request_url:
                evidence_lines.append(f"{fp.request_method} {fp.request_url} → {fp.http_status or '-'}")
            if fp.locator:
                evidence_lines.append(f"定位器：{fp.locator}")
        if evidence_lines:
            lines += ["", "**证据**："] + [f"- {item}" for item in evidence_lines[:5]]

    lines += ["", "**查看详情**："]
    lines.append(f"- 失败中心：{settings.public_base_url}/#/failures")
    if alert.latest_run_id:
        lines.append(f"- 本次执行：{settings.public_base_url}/#/runs/{alert.latest_run_id}")

    content = "\n".join(lines)
    if len(content) > CONTENT_CHAR_LIMIT:
        content = content[:CONTENT_CHAR_LIMIT] + "\n…（内容过长已截断，完整信息见失败中心）"
    return redact_text(content, [settings.wecom_webhook_secret, settings.ai_api_key])


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:40]


def _backoff(attempt: int) -> float:
    return min(2.0 ** (attempt - 1), MAX_BACKOFF_SECONDS)


def send_alert(
    db: Session,
    alert: Alert,
    *,
    provider_name: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """发送一条告警。返回投递结果，**从不修改 alert 本身**。"""
    provider_name = provider_name or default_provider_name()
    try:
        provider = get_provider(provider_name)
    except NotificationProviderError as exc:
        return _record_skipped(db, alert, provider_name, exc.message)

    if not provider.available():
        return _record_skipped(db, alert, provider_name, "通知渠道未配置")

    content = render_alert_message(db, alert)
    content_hash = compute_content_hash(content)
    channel = provider.channel

    row = db.execute(
        select(NotificationLog).where(
            NotificationLog.alert_id == alert.id,
            NotificationLog.channel == channel,
            NotificationLog.content_hash == content_hash,
        )
    ).scalars().first()

    if row is not None and row.status == "SENT" and not force:
        # 同一份内容已经送出去了，不重复发 —— 这是「通知去重」的第二层
        return {
            "alert_id": alert.id,
            "log_id": row.id,
            "status": "SENT",
            "attempt_count": int(row.attempt_count or 0),
            "skipped": True,
            "reason": "同一内容已成功发送过",
        }

    if row is None:
        row = NotificationLog(
            alert_id=alert.id,
            channel=channel,
            provider=provider.name,
            status="PENDING",
            attempt_count=0,
            content_hash=content_hash,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

    message = NotificationMessage(
        title=alert.title or "",
        content=content,
        fields={
            "alert_id": alert.id,
            "failure_group_id": alert.failure_group_id,
            "severity": alert.severity,
            "status": alert.status,
        },
        content_hash=content_hash,
    )

    max_attempts = max(1, int(settings.notification_max_attempts))
    last_error = ""
    sent = False
    for attempt in range(1, max_attempts + 1):
        row.status = "SENDING"
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.provider = provider.name
        db.commit()

        result = provider.send(message)
        if result.ok:
            row.status = "SENT"
            row.message_id = _short(result.message_id, 64)
            row.error_message = ""
            row.sent_at = utcnow()
            db.commit()
            sent = True
            logger.info("告警 %s 通知发送成功（第 %s 次尝试）", alert.id, attempt)
            break

        last_error = result.error
        row.error_message = _short(result.error, 1000)
        db.commit()
        # 只在「值得重试」且还有次数时退避重试
        if result.retryable and attempt < max_attempts:
            time.sleep(_backoff(attempt))
            continue
        break

    if not sent:
        row.status = "FAILED"
        db.commit()
        # 日志里要把「本次投递尝试了几次」和「这一行累计尝试了几次」分开写清楚：
        # 只写累计次数会让人以为一次调用重试了 7 遍，而实际是分了几个投递周期。
        logger.warning(
            "告警 %s 本次通知投递失败（本周期尝试 %s 次，该内容累计 %s 次）：%s",
            alert.id,
            attempt,
            row.attempt_count,
            last_error,
        )

    db.refresh(row)
    return {
        "alert_id": alert.id,
        "log_id": row.id,
        "status": row.status,
        "attempt_count": int(row.attempt_count or 0),
        "skipped": False,
        "message_id": row.message_id or "",
        "error": row.error_message or "",
    }


def _record_skipped(db: Session, alert: Alert, provider_name: str, reason: str) -> dict[str, Any]:
    """渠道不可用时记一条 SKIPPED。

    用 SKIPPED 而不是 FAILED：没配通知是「没开这个功能」，
    不是「发送出错」。把它记成 FAILED 会让运维去查一个根本不存在的故障。
    """
    content_hash = compute_content_hash(f"skipped::{provider_name}::{reason}")
    row = db.execute(
        select(NotificationLog).where(
            NotificationLog.alert_id == alert.id,
            NotificationLog.channel == provider_name,
            NotificationLog.content_hash == content_hash,
        )
    ).scalars().first()
    if row is None:
        row = NotificationLog(
            alert_id=alert.id,
            channel=provider_name,
            provider=provider_name,
            status="SKIPPED",
            attempt_count=0,
            content_hash=content_hash,
            error_message=_short(reason, 1000),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return {
        "alert_id": alert.id,
        "log_id": row.id,
        "status": "SKIPPED",
        "attempt_count": int(row.attempt_count or 0),
        "skipped": True,
        "reason": reason,
    }


def send_alert_async(alert_id: str, *, provider_name: str | None = None, force: bool = False) -> None:
    """异步发送。API 里用它，避免一次企业微信重试把 HTTP 请求拖住 3 秒。"""

    def _worker() -> None:
        from ..db import SessionLocal  # noqa: PLC0415

        db = SessionLocal()
        try:
            alert = db.get(Alert, alert_id)
            if alert is not None:
                send_alert(db, alert, provider_name=provider_name, force=force)
        except Exception:  # noqa: BLE001 - 后台线程只能自己吃掉异常
            logger.exception("异步发送通知失败 alert=%s", alert_id)
        finally:
            db.close()

    threading.Thread(target=_worker, name=f"notify-{alert_id}", daemon=True).start()


def serialize_notification(row: NotificationLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "alert_id": row.alert_id,
        "channel": row.channel,
        "provider": row.provider or "",
        "status": row.status,
        "attempt_count": int(row.attempt_count or 0),
        "message_id": row.message_id or "",
        "error_message": row.error_message or "",
        "sent_at": row.sent_at.isoformat(timespec="seconds") if row.sent_at else None,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
    }


def list_notifications(db: Session, alert_id: str, *, limit: int = 50) -> list[NotificationLog]:
    return list(
        db.execute(
            select(NotificationLog)
            .where(NotificationLog.alert_id == alert_id)
            .order_by(NotificationLog.created_at.desc())
            .limit(limit)
        ).scalars().all()
    )


__all__ = [
    "CONTENT_CHAR_LIMIT",
    "compute_content_hash",
    "list_notifications",
    "render_alert_message",
    "send_alert",
    "send_alert_async",
    "serialize_notification",
]
